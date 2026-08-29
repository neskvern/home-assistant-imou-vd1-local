"""HTTP view serving the raw DHAV video stream to authenticated clients."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class _AiohttpSink:
    """write()/flush() sink that Broadcaster can add_client() with
    directly - it already treats sinks generically, so no changes to
    Broadcaster are needed to plug this in alongside a raw socket."""

    def __init__(self, response: web.StreamResponse, loop: asyncio.AbstractEventLoop) -> None:
        self._response = response
        self._loop = loop

    def write(self, data: bytes) -> None:
        # Runs on the video-worker thread (Broadcaster.write's caller),
        # so bridging onto the event loop is mandatory. Blocking on
        # result() lets a failed write raise here, which Broadcaster's
        # own try/except turns into an automatic remove_client() - no
        # extra plumbing needed in this module for that.
        future = asyncio.run_coroutine_threadsafe(self._response.write(data), self._loop)
        future.result()

    def flush(self) -> None:
        pass  # aiohttp's StreamResponse.write already flushes each chunk


class ImouVd1StreamView(HomeAssistantView):
    """Serves /api/imou_vd1/stream/{entry_id} from a CameraConnection's
    Broadcaster, bridging its synchronous fan-out onto the event loop."""

    url = "/api/imou_vd1/stream/{entry_id}"
    name = "api:imou_vd1:stream"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        conn = self._hass.data.get(DOMAIN, {}).get(entry_id)
        if conn is None:
            return web.Response(status=404)

        response = web.StreamResponse(status=200, headers={"Content-Type": "application/octet-stream"})
        await response.prepare(request)

        loop = asyncio.get_running_loop()
        sink = _AiohttpSink(response, loop)

        conn.broadcaster.add_client(sink)
        try:
            # No disconnect callback exists on the aiohttp side either -
            # this is the same "poll for it" constraint cli.py's own
            # make_stream_handler has (there via MSG_PEEK), here via
            # transport.is_closing().
            while not request.transport.is_closing():
                await asyncio.sleep(1.0)
        finally:
            conn.broadcaster.remove_client(sink)

        return response
