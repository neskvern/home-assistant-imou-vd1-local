# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This repo currently contains a single file, [cli.py](cli.py). There is no build system, package manifest, test suite, or linter config — it's a standalone Python 3 script with zero third-party dependencies (stdlib only). Do not invent build/test/lint tooling that isn't here; if you add tests or dependencies, set up the corresponding manifest/config as part of that change.

The docstring in cli.py refers to "this package", `lib.py`, and an `__init__.py` that import `homeassistant` — these are part of a separate Home Assistant custom-component repo for the Imou VD1 that this script was deliberately extracted from. cli.py is a from-scratch, self-contained reimplementation of that other repo's `CameraConnection` wire protocol, with **zero imports from it**. Keep it that way: don't add an import from `lib.py` or any sibling module even if one is later added to this directory, since the whole point of this file is to run standalone (`python3 cli.py ...`, not `python3 -m ...`, because that package's `__init__.py` pulls in `homeassistant`).

## Running it

```bash
python3 cli.py <host> -p <password> [-u username] [--channel N] [--stream N] \
  [--imou-app-id ID --imou-app-secret SECRET --imou-device-id ID --imou-data-center sg|fk|or] \
  [--listen-host 0.0.0.0] [--listen-port 8080]
```

It wakes the camera (if Imou Cloud credentials are given), then serves the raw DHAV byte stream over plain HTTP at `http://<listen-host>:<listen-port>/` — any number of GET clients can connect concurrently. There's no test suite; verifying a change means running it against a real (or mocked) camera and checking the byte stream / logs on stderr.

## Architecture

Three long-lived daemon threads run for the life of the process (started from `main()`), plus a plain `http.server` for output:

1. **`dvrip_supervisor`** — DVRIP/37777 control connection (login, then `eventManager.attach`). Its only real purpose is a 5s `A1` keepalive (`dvrip_keepalive`) that stops the camera falling asleep; events read off the socket are just logged at debug level, not otherwise used.
2. **`video_worker` → `http_supervisor`** — HTTP/8086 `realmonitor.xav` connection (HTTP Digest auth, `PLAY`). Reads and demuxes the "Private"/DHAV transport framing (`strip_transport_framing`) and pushes payload bytes into a `Broadcaster`. This channel has its own separate 30s keepalive (`http_keepalive`) sent on the video socket itself — required even though the DVRIP channel is also alive, or the stream dies in about a minute.
3. **`run_http_server`** — a `ThreadingHTTPServer` (`make_stream_handler`) that hands each connecting GET client a live tap into the `Broadcaster`.

**`Broadcaster`** (cli.py:575) is the join point: it fans out every write from the video-reading thread to all currently-attached HTTP clients, and toggles a `threading.Event` (`streaming`) on/off as clients connect/disconnect. `stream_to_stdout` (a misnomer now — it writes to whatever `out` sink it's given, here a `Broadcaster`) always keeps draining the camera socket to keep the session warm, but only actually writes bytes out while `streaming` is set — i.e., streaming state is driven entirely by whether any HTTP client is currently attached, not by anything explicit from the camera side.

**Supervisor pattern**: both `dvrip_supervisor` and `http_supervisor` follow the same connect → wake-camera-and-retry-once-on-failure → spawn keepalive thread → run/yield blocking reader → on failure, stop keepalive, close, retry-after-delay, forever loop (see `connect_dvrip_or_wake` / `connect_http_or_wake`). Each channel's socket is shared by its keepalive (writer) thread and its reader thread, so a failure of either side tears down and reconnects both together — never restart just one half.

**Protocol notes worth knowing before touching the wire-level code**:
- HTTP/8086 Digest auth: `HA2` is computed with method `GET`, not `PLAY`, even though the request line itself says `PLAY` (confirmed against a packet capture — see `open_http_connection`).
- The HTTP/8086 channel query param is 1-indexed (`channel + 1`); DVRIP itself is 0-indexed. `--channel`/`--stream` CLI args are DVRIP-style (0-indexed).
- Frame sync on the HTTP/8086 "Private" transport is `0x24` + 1 reserved byte + big-endian uint32 length (`FRAME_MARKER`/`FRAME_HEADER_SIZE`); `strip_transport_framing` also has to detect and discard genuine `HTTP/1.1 ...` responses to the keepalive ping, since those can land interleaved in the same byte stream as frame data.
- Imou Cloud wake-up (`wake_camera`) is entirely optional — it's a no-op unless all three of `--imou-app-id`/`--imou-app-secret`/`--imou-device-id` are supplied — and is only ever used as a fallback after a bare connect attempt fails, never tried first.
