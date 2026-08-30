#!/usr/bin/env python3
"""
Standalone VD1 stream launcher - fully self-contained, no imports from this
package (not even lib.py). Wakes the camera via the Imou Cloud OpenAPI, then
opens a single HTTP/8086 realmonitor.xav connection (HTTP Digest auth, PLAY)
and writes the raw DHAV byte stream to stdout, transport framing stripped -
same wire protocol as lib.py's CameraConnection, reimplemented here from
scratch so this file has zero dependency on the rest of the repo.

No DVRIP control socket and no heartbeat: unlike lib.py, this client does not
keep a control connection open, so the camera may fall back asleep after
~35s of control-channel silence regardless of the video stream still
flowing. A background thread does send the HTTP/8086 subsystem's own
9s keepalive ping on the video socket (see http_keepalive) since that alone is
required just to keep this one connection from being idled out by the
camera - without it the stream dies after roughly a minute even on a
camera that never sleeps.

Diagnostic output goes through the stdlib `logging` module (_LOGGER
below), configured in main() to write to stderr - run as a plain script
(`python3 cli.py ...`), not as a module, since this package's
__init__.py imports `homeassistant`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import re
import socket
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)

IMOU_DATA_CENTERS = {
    "sg": "https://openapi-sg.easy4ip.com:443/openapi",
    "fk": "https://openapi-fk.easy4ip.com:443/openapi",
    "or": "https://openapi-or.easy4ip.com:443/openapi",
}

# Reverse-engineered from a packet capture of the official app: the
# HTTP/8086 video connection has its own idle timeout, separate from
# (and not covered by) any DVRIP control-channel heartbeat.
HTTP_KEEPALIVE_INTERVAL = 30.0

# Without a periodic A1 on the DVRIP control socket, the device falls
# back asleep after a period of silence (observed after ~35s).
DVRIP_KEEPALIVE_INTERVAL = 5.0

# Retry pacing for the channel supervisors (see *_supervisor below): how
# long to wait between reconnect attempts, and how long to wait after a
# cloud wake-up before the device answers on its sockets again.
RETRY_DELAY = 2.0
WAKE_SETTLE_DELAY = 2.0

# Per-chunk framing on the HTTP/8086 "Private" transport: 0x24 sync byte,
# 1 reserved byte, then a big-endian uint32 body length; the body starts
# with the "DHAV" ASCII tag.
FRAME_MARKER = 0x24
FRAME_HEADER_SIZE = 6


# ============================================================
# CLOUD WAKE-UP (Imou OpenAPI)
# ============================================================

def calc_sign(app_secret, timestamp, nonce):
    source = f"time:{timestamp},nonce:{nonce},appSecret:{app_secret}"
    password = hashlib.sha256(app_secret.encode("utf-8")).hexdigest().lower()
    digest = hmac.new(
        password.encode("utf-8"), source.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def call_openapi(base_url, method, app_id, app_secret, params, timeout=10):
    timestamp = int(time.time())
    nonce = str(uuid.uuid4())
    body = {
        "system": {
            "ver": "1.0",
            "appId": app_id,
            "sign": calc_sign(app_secret, timestamp, nonce),
            "time": timestamp,
            "nonce": nonce,
        },
        "id": str(uuid.uuid4()),
        "params": params,
    }
    req = Request(
        f"{base_url}/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            response_body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        response_body = json.loads(e.read().decode("utf-8"))

    result = response_body.get("result", {})
    if result.get("code") != "0":
        raise RuntimeError(
            f"{method} failed: code={result.get('code')} msg={result.get('msg')}"
        )
    return result.get("data", {})


def wake_camera(app_id, app_secret, device_id, data_center):
    if not (app_id and app_secret and device_id):
        return

    base_url = IMOU_DATA_CENTERS[data_center]

    token = call_openapi(base_url, "accessToken", app_id, app_secret, {})["accessToken"]

    call_openapi(base_url, "wakeUpDevice", app_id, app_secret, {
        "token": token,
        "deviceId": device_id,
        "url": "/device/wakeup",
    })


# ============================================================
# HTTP/8086 DIGEST AUTH + STREAM
# ============================================================

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def recv_until(sock, marker=b"\r\n\r\n"):
    data = bytearray()

    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk

    pos = data.find(marker)

    if pos < 0:
        return bytes(data), b""

    end = pos + len(marker)

    return bytes(data[:end]), bytes(data[end:])


def digest_challenge(headers):
    m = re.search(r'WWW-Authenticate:\s*Digest\s+([^\r\n]+)', headers, re.I)

    if not m:
        raise RuntimeError("Did not find Digest challenge")

    fields = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))

    return fields["realm"], fields["nonce"]


def make_http_request(host, port, path, cseq, authorization=None):
    lines = [
        f"PLAY {path} HTTP/1.1",
        "Accpet-Sdp: Private",
    ]

    if authorization:
        lines.append(f"Authorization: {authorization}")

    lines += [
        "Connection: keep-alive",
        f"Cseq: {cseq}",
        f"Host: {host}:{port}",
        "Speed: 1.000000",
        "User-Agent: Http Stream Client/1.0",
        "",
        "",
    ]

    return "\r\n".join(lines).encode()



def strip_transport_framing(buffer):
    """Pop every complete framed chunk off the front of buffer, joined
    into one payload with each chunk's transport header stripped. Any
    trailing partial chunk is left in buffer for the next call. A
    genuine 'HTTP/1.1 ...' reply to our own ping (see ping_loop) can be
    interleaved into the same byte stream and is detected and discarded
    here so it never reaches the caller as bogus frame data."""
    parts = []
    pos = 0
    end_of_buffer = len(buffer)

    while end_of_buffer - pos >= FRAME_HEADER_SIZE:
        if buffer[pos] != FRAME_MARKER:
            if buffer[pos:pos + 5] == b"HTTP/":
                header_end = buffer.find(b"\r\n\r\n", pos)
                if header_end == -1:
                    break  # header not fully buffered yet, wait for more

                pos = header_end + 4
                continue

            raise RuntimeError(f"Unexpected frame marker: {buffer[pos]:#x}")

        frame_len = int.from_bytes(buffer[pos + 2:pos + 6], "big")
        total = FRAME_HEADER_SIZE + frame_len

        if end_of_buffer - pos < total:
            break

        parts.append(buffer[pos + FRAME_HEADER_SIZE:pos + total])
        pos += total

    del buffer[:pos]

    return b"".join(parts)


def open_http_connection(host, port, username, password, channel, stream):
    """Open, Digest-authenticate, and PLAY one HTTP/8086 realmonitor.xav
    connection, then read the SDP. Returns (sock, buffer, ping_path):
    `buffer` holds any DHAV bytes that arrived packed into the same
    read(s) as the SDP, `ping_path` is the query string http_keepalive needs.
    Closes the socket itself and re-raises on any failure along the way."""
    # This HTTP/8086 endpoint is 1-indexed for the channel query
    # parameter, unlike the 0-indexed DVRIP channel/stream convention.
    http_channel = channel + 1

    # method=3 makes the camera close the connection right after
    # replying 200 OK (a teardown); method=2 is the keepalive value
    # observed in real traffic, used here for the ping instead.
    ping_path = f"/live/realmonitor.xav?channel={http_channel}&subtype={stream}&method=2"
    request_path = f"/live/realmonitor.xav?channel={http_channel}&subtype={stream}&method=0"
    # The URI in the Digest header must NOT include method=0.
    digest_uri = f"/live/realmonitor.xav?channel={http_channel}&subtype={stream}"

    sock = socket.create_connection((host, port), timeout=10)

    try:
        # 1. Provoke a Digest challenge
        sock.sendall(make_http_request(host, port, request_path, 0))

        headers, extra = recv_until(sock)
        header_text = headers.decode("latin1", errors="replace")

        if "401" not in header_text:
            raise RuntimeError("Expected HTTP 401")

        realm, nonce = digest_challenge(header_text)

        # 2. Compute Digest response (HA2 uses "GET", not "PLAY", even
        # though the request itself is PLAY - confirmed against a
        # packet capture)
        ha1 = md5(f"{username}:{realm}:{password}")
        ha2 = md5(f"GET:{digest_uri}")
        response = md5(f"{ha1}:{nonce}:{ha2}")

        authorization = (
            f'Digest '
            f'username="{username}", '
            f'realm="{realm}", '
            f'nonce="{nonce}", '
            f'uri="{digest_uri}", '
            f'response="{response}"'
        )

        # 3. Authenticated PLAY
        sock.sendall(make_http_request(host, port, request_path, 1, authorization))

        headers, extra = recv_until(sock)
        header_text = headers.decode("latin1", errors="replace")

        if "200 OK" not in header_text:
            raise RuntimeError("PLAY/authentication failed")

        # 4. Read SDP
        m = re.search(r"Private-Length:\s*(\d+)", header_text, re.I)
        private_length = int(m.group(1)) if m else 0

        buffer = bytearray(extra)

        while len(buffer) < private_length:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed while reading SDP")
            buffer += chunk

        del buffer[:private_length]
    except Exception:
        sock.close()
        raise

    return sock, buffer, ping_path


def http_keepalive(sock, host, port, ping_path, stop_event):
    """Sends a periodic 'PLAY ...' ping on the HTTP/8086 stream socket
    itself, on a separate thread from the one reading it (safe: full
    duplex TCP, one thread only ever writes, the other only reads).
    Stops as soon as stop_event is set, without waiting out the rest of
    the current interval - the supervisor sets it the moment the reader
    side of this same channel dies, so there's no point sending on a
    socket that's about to be replaced."""
    cseq = 2

    while not stop_event.wait(HTTP_KEEPALIVE_INTERVAL):
        try:            
            lines = [
                f"PLAY {ping_path} HTTP/1.1",
                "Accpet-Sdp: Private",
                "Connection: keep-alive",
                f"Cseq: {cseq}",
                f"Host: {host}:{port}",
                "User-Agent: Http Stream Client/1.0",
                "",
                "",
            ]

            sock.sendall("\r\n".join(lines).encode())
            _LOGGER.debug("HTTP: KeepAlive packet sent")
            
            cseq += 1
        except Exception:
            break


def stream_to_stdout(sock, buffer, out, streaming_event):
    """Keeps reading and demuxing frames until the connection dies,
    regardless of streaming_event - only writes them to out while
    streaming_event is set. The toggle mutes/unmutes output; it never
    stops draining the socket, so the video session (and the camera's
    video subsystem) stays warm the whole time, independent of whether
    anyone is actually consuming the output right now."""
    try:
        while True:
            payload = strip_transport_framing(buffer)
            if payload and streaming_event.is_set():
                out.write(payload)
                out.flush()                

            chunk = sock.recv(64 * 1024)
            if not chunk:
                break
            buffer += chunk
    finally:
        sock.close()


# ============================================================
# DVRIP EVENTS (eventManager.attach on port 37777, F6 JSON framing)
# ============================================================

A1_KEEPALIVE = b"\xa1" + (b"\x00" * 31)


def dvrip_keepalive(sock, stop_event):
    """Same stop_event reasoning as http_keepalive above."""
    while not stop_event.wait(DVRIP_KEEPALIVE_INTERVAL):
        try:
            sock.sendall(A1_KEEPALIVE)
            _LOGGER.debug("DVRIP: KeepAlive packet sent")                        
        except Exception:
            break


def compressor(data):
    out = []
    for i in range(len(data) // 2):
        value = (data[2 * i] + data[2 * i + 1]) % 62
        if value < 10:
            value += 48
        elif value < 36:
            value += 55
        else:
            value += 61
        out.append(value)
    return out


def dvrip_md5_hash(random_value, username, password):
    gen1 = "".join(chr(x) for x in compressor(hashlib.md5(password.encode("latin-1")).digest()))
    value = f"{username}:{random_value}:{gen1}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def gen2_md5_hash(random_value, realm, username, password):
    inner = hashlib.md5(f"{username}:{realm}:{password}".encode("latin-1")).hexdigest().upper()
    value = f"{username}:{random_value}:{inner}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Camera closed connection")
        data.extend(chunk)
    return bytes(data)


def dvrip_login(sock, username, password):
    sock.sendall(b"\xa0\x01\x00\x00" + (b"\x00" * 20) + bytes.fromhex("05 02 01 01 00 00 a1 aa"))
    text = sock.recv(4096)[32:].decode("latin-1", errors="ignore").strip("\x00")

    realm = random_value = None
    for line in text.split("\r\n"):
        if line.startswith("Realm:"):
            realm = line[len("Realm:"):]
        elif line.startswith("Random:"):
            random_value = line[len("Random:"):]

    payload = (
        username + "&&"
        + gen2_md5_hash(random_value, realm, username, password)
        + dvrip_md5_hash(random_value, username, password)
    ).encode("latin-1")

    header = (
        b"\xa0\x05\x00\x60" + struct.pack("<I", len(payload)) + (b"\x00" * 16)
        + bytes.fromhex("05 02 00 08 00 00 a1 aa")
    )
    sock.sendall(header + payload)
    response = sock.recv(4096)

    if response[8:10] != b"\x00\x08":
        raise RuntimeError("DVRIP login failed")

    return struct.unpack("<I", response[16:20])[0]  # session


def open_dvrip_connection(host, port, username, password):
    """Open and log into one DVRIP/37777 control connection. Returns
    (sock, session) - the DVRIP analogue of open_http_connection()."""
    sock = socket.create_connection((host, port), timeout=10)
    session = dvrip_login(sock, username, password)
    sock.settimeout(None)  # block indefinitely between events

    return sock, session


def listen_events(sock, session):
    """Attach to the camera's eventManager (codes=All) and print
    "[Event]" to stderr for every pushed event, forever."""
    payload = json.dumps(
        {"id": 1, "method": "eventManager.attach", "params": {"codes": ["SmartMotionHuman"]}, "session": session},
        separators=(",", ":"),
    ).encode("latin-1") + b"\n\x00"

    sock.sendall(
        struct.pack("<IIIIIIII", 0xF6, len(payload), 1, 0, len(payload), 0, session, 0) + payload
    )

    while True:
        first = recv_exact(sock, 2)
        if first != b"\xf6\x00":
            recv_exact(sock, 30)
            continue

        length = struct.unpack("<I", recv_exact(sock, 30)[2:6])[0]
        body = recv_exact(sock, length)

        if b'"result"' not in body:  # skip the attach ack itself
            _LOGGER.debug("Event: %s", body)


# ============================================================
# CHANNEL SUPERVISORS
#
# Each channel's socket is shared by a writer (keepalive) and a reader
# thread (listen_events / stream_to_stdout), so a failure of either one
# means the whole channel - both threads and the socket - has to be torn
# down and reopened together; restarting only the failed half would
# leave the other half stuck on a dead or since-replaced socket. Each
# supervisor loops forever: connect (bare attempt, wake-and-retry once
# on failure - same cheap-path-first approach as lib.py's
# dvrip_connect_or_wake/http_connect_or_wake), start the keepalive
# thread, run the reader (blocking), then on any failure stop the
# keepalive and reconnect - forever, until the process itself is killed.
# ============================================================

def connect_dvrip_or_wake(args):
    try:
        return open_dvrip_connection(args.host, args.dvrip_port, args.username, args.password)
    except Exception:
        wake_camera(args.imou_app_id, args.imou_app_secret, args.imou_device_id, args.imou_data_center)
        time.sleep(WAKE_SETTLE_DELAY)
        return open_dvrip_connection(args.host, args.dvrip_port, args.username, args.password)


def connect_http_or_wake(args):
    try:
        return open_http_connection(
            args.host, args.http_port, args.username, args.password, args.channel, args.stream
        )
    except Exception:
        wake_camera(args.imou_app_id, args.imou_app_secret, args.imou_device_id, args.imou_data_center)
        time.sleep(WAKE_SETTLE_DELAY)
        return open_http_connection(
            args.host, args.http_port, args.username, args.password, args.channel, args.stream
        )


def dvrip_supervisor(args):
    while True:
        try:
            sock, session = connect_dvrip_or_wake(args)
        except Exception:
            time.sleep(RETRY_DELAY)
            continue

        stop_event = threading.Event()
        threading.Thread(
            target=dvrip_keepalive,
            args=(sock, stop_event),
            daemon=True,
        ).start()

        try:
            listen_events(sock, session)  # blocks until the channel dies
        except Exception:
            pass

        stop_event.set()
        sock.close()
        time.sleep(RETRY_DELAY)


def http_supervisor(args):
    """Keeps the HTTP/8086 video channel connected forever: connect
    (bare attempt, wake-and-retry once on failure), start the keepalive
    thread, then yield (sock, buffer) for the caller to read from.
    Resumes once the caller's `for` loop advances - after it's done
    with, or given up on, that connection - tearing down the keepalive
    and reconnecting before yielding the next one."""
    while True:
        try:
            sock, buffer, ping_path = connect_http_or_wake(args)
        except Exception:
            time.sleep(RETRY_DELAY)
            continue

        stop_event = threading.Event()
        threading.Thread(
            target=http_keepalive,
            args=(sock, args.host, args.http_port, ping_path, stop_event),
            daemon=True,
        ).start()

        yield sock, buffer

        stop_event.set()
        time.sleep(RETRY_DELAY)


# ============================================================
# HTTP SERVER (serves the video stream to any GET client)
# ============================================================

class Broadcaster:
    """A write()/flush() sink - drops straight into stream_to_stdout's
    `out` parameter in place of a single file - that fans each write
    out to every currently connected GET client instead.

    Also owns `streaming`, a threading.Event that's set while at least
    one client is connected and cleared the moment the last one
    disconnects - stream_to_stdout uses it to only write frames while
    someone's actually listening, so a client connecting/disconnecting
    is what starts/stops streaming (replaces the old keypress toggle)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []
        self.streaming = threading.Event()

    def add_client(self, wfile):
        with self._lock:
            self._clients.append(wfile)
            self.streaming.set()
        _LOGGER.info("Broadcaster: HTTP client connected")

    def remove_client(self, wfile):
        with self._lock:
            if wfile in self._clients:
                self._clients.remove(wfile)
                _LOGGER.info("Broadcaster: HTTP client disconnected")
            if not self._clients:
                self.streaming.clear()
                _LOGGER.info("Broadcaster: Streaming cleared")
        

    def write(self, data):
        with self._lock:
            clients = list(self._clients)

        for wfile in clients:
            try:
                wfile.write(data)
            except Exception:
                self.remove_client(wfile)

    def flush(self):
        with self._lock:
            clients = list(self._clients)

        for wfile in clients:
            try:
                wfile.flush()
            except Exception:
                self.remove_client(wfile)


def make_stream_handler(broadcaster):
    class StreamRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()

            broadcaster.add_client(self.wfile)
            self.connection.settimeout(1.0)

            try:
                # Nothing else reads this connection, so the only way to
                # notice the client went away is to poll for it - a
                # closed/reset socket makes recv() return b"" or raise.
                while True:
                    try:
                        if not self.connection.recv(1, socket.MSG_PEEK):
                            break
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            finally:
                broadcaster.remove_client(self.wfile)

        def log_message(self, format, *args):
            _LOGGER.debug(format, *args)

    return StreamRequestHandler


def run_http_server(host, port, broadcaster):
    ThreadingHTTPServer((host, port), make_stream_handler(broadcaster)).serve_forever()


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Wake the VD1, open a single HTTP/8086 video connection, and "
            "write the raw DHAV stream to stdout."
        )
    )

    parser.add_argument("host")

    parser.add_argument("-u", "--username", default="admin")
    parser.add_argument("-p", "--password", required=True)

    parser.add_argument("--dvrip-port", type=int, default=37777)
    parser.add_argument("--http-port", type=int, default=8086)

    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--stream", type=int, default=0)

    parser.add_argument("--imou-app-id")
    parser.add_argument("--imou-app-secret")
    parser.add_argument("--imou-device-id")
    parser.add_argument("--imou-data-center", default="fk", choices=sorted(IMOU_DATA_CENTERS))

    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8080)

    return parser.parse_args()


def video_worker(args, broadcaster):
    """Keeps the HTTP video channel connected and drained forever
    (reconnecting via http_supervisor if it drops) regardless of whether
    anyone's connected - only whether frames get written out depends on
    broadcaster.streaming, which Broadcaster itself sets/clears as GET
    clients come and go. Runs for the whole lifetime of the program,
    same as dvrip_supervisor."""
    for sock, buffer in http_supervisor(args):
        try:
            stream_to_stdout(sock, buffer, broadcaster, broadcaster.streaming)
        except Exception:
            pass


def main():
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(message)s")

    args = parse_args()

    wake_camera(args.imou_app_id, args.imou_app_secret, args.imou_device_id, args.imou_data_center)

    threading.Thread(
        target=dvrip_supervisor,
        args=(args,),
        daemon=True,
    ).start()

    broadcaster = Broadcaster()

    threading.Thread(
        target=video_worker,
        args=(args, broadcaster),
        daemon=True,
    ).start()

    threading.Thread(
        target=run_http_server,
        args=(args.listen_host, args.listen_port, broadcaster),
        daemon=True,
    ).start()

    _LOGGER.info("Main: Serving video on http://%s:%s/", args.listen_host, args.listen_port)

    threading.Event().wait()


if __name__ == "__main__":
    main()
