"""Bridge one EZVIZ cloud playback session to Home Assistant's HLS worker."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from hmac import compare_digest
import logging
from secrets import token_urlsafe

from aiohttp import web
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.capture import AutoClipSource
from pyezvizapi.streaming.live import (
    LiveMpegtsStream,
    LiveStreamShutdownTimeout,
    open_live_mpegts_stream,
)

from homeassistant.core import HomeAssistant, callback

from .const import CONF_RF_SESSION_ID
from .workers import create_critical_task

_LOGGER = logging.getLogger(__name__)
_HOST = "127.0.0.1"
_READ_TIMEOUT = 15
_CLEANUP_WAIT = 1
_PLAYBACK_OWNERS = "ezviz_cloud_playback_owners"


@dataclass(frozen=True, slots=True)
class CloudStreamRoute:
    """A specific cloud channel, quality and current media credential."""

    channel: int
    stream_index: int
    media_key: str | None = field(repr=False)


class CloudStreamRelay:
    """Serve a single bounded reader on an ephemeral loopback listener."""

    def __init__(
        self,
        hass: HomeAssistant,
        account: EzvizClient,
        serial: str,
        ffmpeg_binary: str,
        route: Callable[[], CloudStreamRoute | None],
        on_request: Callable[[], None] | None = None,
    ) -> None:
        """Create an inert relay; neither a listener nor media are opened here."""
        self._hass = hass
        self._account = account
        self._serial = serial
        self._ffmpeg_binary = ffmpeg_binary
        self._route = route
        self._on_request = on_request
        self._owners: set[tuple[str, int]] = hass.data.setdefault(
            _PLAYBACK_OWNERS, set()
        )
        self._secret = token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._url: str | None = None
        self._handler: asyncio.Task | None = None
        self._active_route: CloudStreamRoute | None = None
        self._cleanup: asyncio.Task | None = None
        self._failed_cleanup = False

    async def async_start(self) -> str:
        """Start the private HTTP endpoint without requesting camera media."""
        if self._url is None:
            app = web.Application()
            app.router.add_get("/live.ts", self._async_serve, allow_head=False)
            runner = web.AppRunner(
                app, access_log=None, handler_cancellation=True, shutdown_timeout=1
            )
            await runner.setup()
            try:
                await web.TCPSite(runner, _HOST, 0).start()
            except OSError, asyncio.CancelledError:
                await runner.cleanup()
                raise
            self._runner = runner
            self._url = (
                f"http://{_HOST}:{runner.addresses[0][1]}/live.ts?auth={self._secret}"
            )
        return self._url

    @callback
    def async_invalidate(self) -> None:
        """Discard a reader if its exact route or credential is no longer current."""
        if self._handler is not None and self._active_route != self._route():
            self._handler.cancel()

    async def async_stop(self) -> None:
        """Revoke the endpoint, cancel in-flight delivery and join the SDK owner."""
        if self._runner is not None:
            self._url = None
            if self._handler is not None:
                self._handler.cancel()
            await self._runner.cleanup()
            self._runner = None
        if self._cleanup is not None:
            await asyncio.wait((self._cleanup,), timeout=_CLEANUP_WAIT)

    def _open(self, route: CloudStreamRoute) -> tuple[EzvizClient, LiveMpegtsStream]:
        """Use a playback-only snapshot; only the account owner may rotate tokens."""
        token = self._account.export_token()
        token.pop(CONF_RF_SESSION_ID, None)
        client = EzvizClient(token=token, feature_code=self._account.feature_code)
        try:
            reader = open_live_mpegts_stream(
                client,
                self._serial,
                source=AutoClipSource(mode="cloud"),
                channel=route.channel,
                ffmpeg_path=self._ffmpeg_binary,
                media_key=route.media_key,
                cloud_stream_index=route.stream_index,
            )
        except PyEzvizError, OSError, ValueError:
            client.close_session()
            raise
        return client, reader

    async def _async_serve(self, request: web.Request) -> web.StreamResponse:
        """Admit only the private Core consumer and write with HTTP backpressure."""
        if not compare_digest(
            request.query.get("auth", "").encode(), self._secret.encode()
        ):
            raise web.HTTPNotFound
        if self._on_request is not None:
            self._on_request()
        if (
            self._url is None
            or self._failed_cleanup
            or (route := self._route()) is None
        ):
            raise web.HTTPServiceUnavailable
        owner = (self._serial, route.channel)
        if (
            self._handler is not None
            or self._cleanup is not None
            or owner in self._owners
        ):
            raise web.HTTPConflict

        self._owners.add(owner)
        self._handler = asyncio.current_task()
        self._active_route = route
        opening = self._hass.async_add_executor_job(self._open, route)
        response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        reading = None
        # A blocked reader cannot occupy the executor needed to interrupt it.
        read_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ezviz-read"
        )
        try:
            _, reader = await asyncio.shield(opening)
            while True:
                async with asyncio.timeout(_READ_TIMEOUT):
                    reading = self._hass.loop.run_in_executor(
                        read_executor, reader.read
                    )
                    chunk = await asyncio.shield(reading)
                if not chunk:
                    break
                if not response.prepared:
                    await response.prepare(request)
                await response.write(chunk)
        except (PyEzvizError, OSError, ValueError) as err:
            _LOGGER.debug("EZVIZ cloud playback ended (%s)", type(err).__name__)
        finally:
            self._cleanup = create_critical_task(
                self._hass,
                self._async_finish(opening, reading, read_executor, owner),
                "Close EZVIZ cloud playback",
            )
            self._handler = None
            self._active_route = None
            await asyncio.wait((self._cleanup,), timeout=_CLEANUP_WAIT)

        if not response.prepared:
            raise web.HTTPBadGateway
        return response

    async def _async_finish(
        self,
        opening: asyncio.Future[tuple[EzvizClient, LiveMpegtsStream]],
        reading: asyncio.Future[bytes] | None,
        read_executor: ThreadPoolExecutor,
        owner: tuple[str, int],
    ) -> None:
        """Finish any abandoned open/read before releasing the playback slot."""
        try:
            client, reader = await asyncio.shield(opening)
        except PyEzvizError, OSError, ValueError:
            return
        else:
            try:
                while True:
                    try:
                        await self._hass.async_add_executor_job(reader.close)
                        break
                    except LiveStreamShutdownTimeout:
                        # SDK retains ownership; wait on the same reader, never reopen.
                        continue
            except (PyEzvizError, OSError) as err:
                self._failed_cleanup = True
                _LOGGER.warning(
                    "EZVIZ cloud stream cleanup failed (%s)", type(err).__name__
                )
            finally:
                if reading is not None:
                    with suppress(PyEzvizError, OSError, ValueError):
                        await asyncio.shield(reading)
                await self._hass.async_add_executor_job(client.close_session)
        finally:
            read_executor.shutdown(wait=False)
            if not self._failed_cleanup:
                self._owners.discard(owner)
            self._cleanup = None
