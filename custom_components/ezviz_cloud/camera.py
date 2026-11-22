"""Support EZVIZ camera devices.

This module exposes Home Assistant camera entities based on data provided by the
cloud-scoped EZVIZ config entry. There are no per-camera config entries or discovery
flows. Per-camera configuration is read from the cloud entry's options at:

    entry.options["cameras"][<SERIAL>]

The RTSP stream path is stored per camera as `rtsp_stream_path`.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from io import BytesIO
import logging
from typing import Any, override
from urllib.parse import quote, urlsplit
from weakref import WeakSet

from PIL import Image
from pyezvizapi.devices.channels import (
    CameraChannel,
    camera_channels,
    capture_channel_number,
    reported_video_channel_numbers,
    resolve_camera_channel,
)
from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

from homeassistant.components import ffmpeg
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    get_dynamic_camera_stream_settings,
)
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import (
    CONF_USE_WALLCLOCK_AS_TIMESTAMPS,
    HLS_PROVIDER,
    RECORDER_PROVIDER,
    Stream,
    StreamOutput,
    create_stream,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .availability import device_available
from .channels import (
    async_add_channel_entities,
    capture_signal,
    channel_available,
    is_additional_camera_lens,
    lens_stream_path,
    registered_lens_serials,
    reported_lens_channels,
)
from .cloud_stream import CloudStreamRelay, CloudStreamRoute
from .const import (
    CLOUD_STREAM_INDEXES,
    CONF_ENC_KEY,
    CONF_RTSP_STREAM_PATH,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_STREAM_SOURCE,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_RTSP_STREAM_PATH,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
)
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .errors import request_error
from .image import async_channel_image
from .ptz import point_route
from .runtime import EzvizConfigEntry
from .workers import create_critical_task

_LOGGER = logging.getLogger(__name__)

# HA gives a camera image request 10 seconds. Reserve most of it for a cloud still.
RTSP_PREVIEW_TIMEOUT = 4
STREAM_STOP_REASSERT_INTERVAL = 0.05
CLOUD_RECORDING_STARTUP_TIMEOUT = 30


def _preview_jpeg(body: bytes) -> bytes | None:
    """Encode non-JPEG cloud previews for HA's multipart still stream."""
    try:
        with Image.open(BytesIO(body)) as image, BytesIO() as output:
            image.convert("RGB").save(output, format="JPEG")
            return output.getvalue()
    except OSError, Image.DecompressionBombError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ cameras for a cloud config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    cams_opts: Mapping[str, dict[str, Any]] = entry.options[OPTIONS_KEY_CAMERAS]

    entities: list[EzvizCamera] = []
    for serial in coordinator.data:
        per_cam: dict[str, Any] = cams_opts.get(serial, {})

        username: str = per_cam.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME)

        use_vc: bool = bool(per_cam.get(CONF_RTSP_USES_VERIFICATION_CODE, False))
        enc_key: str = per_cam.get(CONF_ENC_KEY, "")
        password: str = enc_key if not use_vc else per_cam.get(CONF_PASSWORD, "")

        rtsp_path: str | None = per_cam.get(
            CONF_RTSP_STREAM_PATH, DEFAULT_RTSP_STREAM_PATH
        )

        if rtsp_path and not rtsp_path.startswith("/"):
            # Be lenient: if users saved path without leading slash, add it.
            rtsp_path = "/" + rtsp_path

        if not password and per_cam.get(CONF_STREAM_SOURCE) not in CLOUD_STREAM_INDEXES:
            _LOGGER.warning(
                "Camera %s missing RTSP password%s; stream may be unavailable until provided",
                serial,
                " (verification code expected for RTSP)"
                if use_vc
                else " (encryption code expected for RTSP)",
            )

        entities.append(
            EzvizCamera(
                hass=hass,
                coordinator=coordinator,
                serial=serial,  # canonical unique_id
                camera_username=username,
                camera_password=password,
                rtsp_path=rtsp_path,
            )
        )

    async_add_entities(entities)
    async_add_channel_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda serial, channel: (
            [EzvizChannelCamera(hass, coordinator, serial, channel)]
            if is_additional_camera_lens(channel)
            else []
        ),
    )


class EzvizCamera(EzvizEntity, Camera):
    """EZVIZ security camera entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        camera_username: str,
        camera_password: str,
        rtsp_path: str | None,
        *,
        channel: CameraChannel | None = None,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__(coordinator, serial)
        Camera.__init__(self)
        self.stream: Stream | None = None
        self._stopped_stream: Stream | None = None
        self._stream_teardown: asyncio.Task[None] | None = None

        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True

        self._username: str = camera_username
        self._password: str = camera_password
        self._rtsp_path: str | None = rtsp_path
        self._ffmpeg = get_ffmpeg_manager(hass)
        self._attr_unique_id = serial
        self._channel_key = channel.key if channel is not None else None
        self._channel_image_key: tuple[Any, ...] | None = None
        self._channel_image: bytes | None = None
        self._preview_tasks: dict[
            tuple[str, int | None, int | None], asyncio.Task[bytes | None]
        ] = {}
        if channel is None:
            self._attr_name = None
        self._stream_lifecycle_lock = asyncio.Lock()
        self._removed = False
        entry = coordinator.config_entry
        options = (
            entry.options.get(OPTIONS_KEY_CAMERAS, {}).get(serial, {}) if entry else {}
        )
        self._cloud_stream_index = CLOUD_STREAM_INDEXES.get(
            options.get(CONF_STREAM_SOURCE, "")
        )
        self._cloud_primary_seen = False
        self._watched_recorders: WeakSet[StreamOutput] = WeakSet()
        self._cloud_relay = (
            CloudStreamRelay(
                hass,
                coordinator.ezviz_client,
                serial,
                self._ffmpeg.binary,
                self._cloud_route,
                self._arm_cloud_startup_timeout,
            )
            if self._cloud_stream_index is not None
            else None
        )
        self._rtsp_stream: str = self._build_rtsp()

    def _cloud_route(self) -> CloudStreamRoute | None:
        """Read the exact lens and credential at admission, not at option save."""
        if (
            self._cloud_stream_index is None
            or self._removed
            or not device_available(self.coordinator, self._serial)
            or self.coordinator.shutting_down
        ):
            return None
        if self._channel_key is None:
            numbers = reported_video_channel_numbers(self.data)
            if 1 in numbers:
                self._cloud_primary_seen = True
                channel = 1
            elif numbers == (0,) and not self._cloud_primary_seen:
                # Channel 0 is provisional until we see lens 1 or know that
                # the device already has a separately registered lens.
                entry = self.coordinator.config_entry
                if entry is not None and self._serial in registered_lens_serials(
                    self.coordinator, entry
                ):
                    return None
                channel = 0
            else:
                return None
        else:
            try:
                channel = resolve_camera_channel(
                    self.data, self._channel_key
                ).channel_no
            except PyEzvizError:
                return None
        media_key = None
        if self.data.get("encrypted"):
            media_key = self.coordinator.credentials.key(self._serial)
            if not media_key:
                return None
        return CloudStreamRoute(channel, self._cloud_stream_index, media_key)

    @callback
    def _arm_cloud_startup_timeout(self) -> None:
        """Bound cloud startup even when no segment arrives."""
        if (stream := self.stream) is None:
            return
        if (
            not stream.dynamic_stream_settings.preload_stream
            and (output := stream.outputs().get(HLS_PROVIDER)) is not None
        ):
            output.idle_timer.start()
        self._arm_cloud_recorder_timeout(stream)

    @callback
    def _arm_cloud_recorder_timeout(self, stream: Stream) -> None:
        """Watch for the first recording segment without starting its duration."""
        if (
            self.stream is stream
            and stream is not None
            and (recorder := stream.outputs().get(RECORDER_PROVIDER)) is not None
            and recorder not in self._watched_recorders
        ):
            self._watched_recorders.add(recorder)
            if recorder.last_segment is None:
                self.hass.async_create_background_task(
                    self._async_bound_recorder_startup(stream, recorder),
                    "EZVIZ cloud recording startup",
                )

    async def _async_bound_recorder_startup(
        self, stream: Stream, recorder: StreamOutput
    ) -> None:
        try:
            async with asyncio.timeout(CLOUD_RECORDING_STARTUP_TIMEOUT):
                if recorder.last_segment is None:
                    await recorder.recv()
        except TimeoutError:
            if (
                stream.outputs().get(RECORDER_PROVIDER) is recorder
                and recorder.last_segment is None
            ):
                await stream.remove_provider(recorder)

    async def _async_create_cloud_stream(
        self, relay: CloudStreamRelay
    ) -> Stream | None:
        """Use the Core HLS worker without publishing a WebRTC source URL."""
        if self._cloud_route() is None:
            return None
        if self.stream is None:
            settings = await get_dynamic_camera_stream_settings(
                self.hass, self.entity_id
            )
            source = await relay.async_start()
            if self._cloud_route() is None:
                return None
            self.stream = create_stream(
                self.hass,
                source,
                options={},
                dynamic_stream_settings=settings,
                stream_label=self.entity_id,
            )
            self.stream.set_update_callback(self.async_write_ha_state)
        # Core's record service adds its output immediately after obtaining the
        # stream, including when it joins an already-open relay connection.
        self.hass.loop.call_soon(self._arm_cloud_recorder_timeout, self.stream)
        return self.stream

    def _build_rtsp(self) -> str:
        """Build an RTSP URL from coordinator data and per-camera credentials.

        Returns:
            str
            RTSP URL in the form:'rtsp://<user>:<pass>@<ip>:<port><path>'
        """
        if entry := self.coordinator.config_entry:
            options = entry.options.get(OPTIONS_KEY_CAMERAS, {}).get(self._serial)
            if options is not None:
                self._username = options.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME)
                field = (
                    CONF_PASSWORD
                    if options.get(CONF_RTSP_USES_VERIFICATION_CODE, False)
                    else CONF_ENC_KEY
                )
                self._password = options.get(field, "")
                self._rtsp_path = options.get(
                    CONF_RTSP_STREAM_PATH, DEFAULT_RTSP_STREAM_PATH
                )
        ip = self.data["local_ip"]
        port = self.data["local_rtsp_port"]
        path = self._rtsp_path
        if self._channel_key is not None:
            entry = self.coordinator.config_entry
            options = (
                entry.options.get(OPTIONS_KEY_CAMERAS, {}).get(self._serial, {})
                if entry
                else {}
            )
            try:
                channel = resolve_camera_channel(self.data, self._channel_key)
                path = lens_stream_path(self.data, options, channel)
            except PyEzvizError:
                path = ""
            if not path:
                return ""
        if path is None:
            return ""
        if path and not path.startswith("/"):
            path = "/" + path
        return f"rtsp://{quote(self._username, safe='')}:{quote(self._password, safe='')}@{ip}:{port}{path}"

    @override
    async def async_added_to_hass(self) -> None:
        """Refresh the source when added or re-added after an options reload."""
        self._removed = False
        await super().async_added_to_hass()
        if self._cloud_relay is not None:
            self.async_on_remove(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._async_stop_cloud_on_shutdown
                )
            )
        # OptionsFlowWithReload owns form changes; entry update listeners are
        # incompatible with it. Credential actions notify the coordinator after
        # committing, so they still update an existing stream without a reload.
        self._refresh_stream_source()

    @callback
    def _refresh_stream_source(self) -> None:
        """Restart an existing stream only when its selected source changes."""
        if self._removed or self.coordinator.shutting_down:
            return
        if self._cloud_relay is not None:
            self._cloud_relay.async_invalidate()
            return
        source = self._build_rtsp()
        if source != self._rtsp_stream:
            self._rtsp_stream = source
            if self.stream is not None:
                if source:
                    self.stream.update_source(source)
                else:
                    self.hass.async_create_task(
                        self._async_stop_missing_stream(),
                        "Stop unavailable EZVIZ stream",
                    )

    async def _async_stop_missing_stream(self) -> None:
        """Stop the worker without handing it an empty source."""
        async with self._stream_lifecycle_lock:
            await self._async_wait_for_stream_teardown()
            if not self._removed and not self._build_rtsp():
                await self._async_stop_stream(reuse=True)

    async def _async_stop_stream(self, *, reuse: bool = False) -> None:
        if (stream := self.stream) is not None:
            self.stream = None
            self._stream_teardown = create_critical_task(
                self.hass,
                self._async_finish_stream_teardown(stream, reuse=reuse),
                "EZVIZ stream teardown",
            )
            await self._async_wait_for_stream_teardown()

    async def _async_wait_for_stream_teardown(self) -> None:
        """Wait for detached teardown before making another lifecycle change."""
        if (teardown := self._stream_teardown) is not None:
            await asyncio.shield(teardown)

    async def _async_finish_stream_teardown(
        self, stream: Stream, *, reuse: bool
    ) -> None:
        try:
            settings = stream.dynamic_stream_settings
            stream.dynamic_stream_settings = replace(settings, preload_stream=False)
            outputs = stream.outputs()
            await self._async_stop_stream_worker(stream)
            if self._cloud_relay is not None:
                # Core retains stopped Stream objects until HA shutdown.
                stream.set_update_callback(lambda: None)
            # Core drops the outputs without cancelling their idle timers. An
            # old timer must not remove a replacement HLS output on this stream.
            for output in outputs.values():
                output.cleanup()
            if reuse and not self._removed:
                stream.dynamic_stream_settings = settings
                self._stopped_stream = stream
        finally:
            self._stream_teardown = None

    async def _async_stop_stream_worker(self, stream: Stream) -> None:
        """Keep Core's quit signal set until its worker joins."""
        # Core's update_source() arms a one-shot restart. The worker may
        # already be past the flag check and about to clear the quit event.
        stream._fast_restart_once = False  # noqa: SLF001
        quit_event = stream._thread_quit  # noqa: SLF001
        loop = asyncio.get_running_loop()
        retry: asyncio.TimerHandle

        def reassert_stop() -> None:
            nonlocal retry
            quit_event.set()
            retry = loop.call_later(STREAM_STOP_REASSERT_INTERVAL, reassert_stop)

        retry = loop.call_later(STREAM_STOP_REASSERT_INTERVAL, reassert_stop)
        try:
            await stream.stop()
        finally:
            retry.cancel()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Apply key commits and address changes to an already created stream."""
        if not self._removed:
            self._refresh_stream_source()
            super()._handle_coordinator_update()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Fence queued updates before stopping the owned stream."""
        await self._async_remove_stream()
        await super().async_will_remove_from_hass()

    async def _async_stop_cloud_on_shutdown(self, _event: Any) -> None:
        """Release the private listener even when HA stops without unloading."""
        await self._async_remove_stream()

    async def _async_remove_stream(self) -> None:
        self._removed = True
        async with self._stream_lifecycle_lock:
            await self._async_wait_for_stream_teardown()
            if self._cloud_relay is not None:
                await self._cloud_relay.async_stop()
            self._stopped_stream = None
            await self._async_stop_stream()

    @override
    async def async_create_stream(self) -> Stream | None:
        """Serialize stream creation with removal and refresh after creation awaits."""
        async with self._stream_lifecycle_lock:
            await self._async_wait_for_stream_teardown()
            if self._removed or self.coordinator.shutting_down:
                return None
            if self._cloud_relay is not None:
                return await self._async_create_cloud_stream(self._cloud_relay)
            # Only additional lenses require current cloud health; the RTSP
            # builder below verifies the exact route as well as the path.
            if self._channel_key is not None and not device_available(
                self.coordinator, self._serial
            ):
                return None
            if not (source := self._build_rtsp()):
                await self._async_stop_stream(reuse=True)
                return None
            if (stream := self._stopped_stream) is not None:
                self._stopped_stream = None
                # No worker to restart; update_source would arm a restart.
                stream.source = source
                self.stream = stream
            else:
                stream = await super().async_create_stream()
            # Removal or lens health may have changed while HA awaited preferences.
            if self._removed or self.coordinator.shutting_down:
                await self._async_stop_stream()
                return None
            if stream is not None:
                source = self._build_rtsp()
                if not source or (self._channel_key is not None and not self.available):
                    await self._async_stop_stream(reuse=True)
                    return None
                if stream.source != source:
                    stream.update_source(source)
                self._rtsp_stream = source
            return stream

    @property
    @override
    def is_recording(self) -> bool:
        """Return True if the device is currently recording."""
        return bool(self.data["alarm_notify"])

    @property
    @override
    def motion_detection_enabled(self) -> bool:
        """Return True if motion detection is enabled."""
        return bool(self.data["alarm_notify"])

    @override
    async def async_enable_motion_detection(self) -> None:
        """Enable motion detection (a.k.a. defence) on the device."""
        try:
            await self.async_write_setting(
                self.coordinator.ezviz_client.set_camera_defence,
                self._serial,
                1,
            )

        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    @override
    async def async_disable_motion_detection(self) -> None:
        """Disable motion detection (a.k.a. defence) on the device."""
        try:
            await self.async_write_setting(
                self.coordinator.ezviz_client.set_camera_defence,
                self._serial,
                0,
            )

        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    @property
    @override
    def supported_features(self) -> CameraEntityFeature:
        """Advertise streaming only when this camera has a source."""
        return (
            CameraEntityFeature.STREAM
            if self._cloud_relay is not None or self._build_rtsp()
            else CameraEntityFeature(0)
        )

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return an RTSP frame or a verified cloud still for this lens."""
        if self._removed or self.coordinator.shutting_down:
            return None
        if self._channel_key is not None and not self.available:
            return None
        if rtsp_source := self._build_rtsp():
            # The original camera has no RTSP-to-cloud fallback to budget for.
            timeout = RTSP_PREVIEW_TIMEOUT if self._channel_key is not None else None
            try:
                async with asyncio.timeout(timeout):
                    frame = await self._async_rtsp_preview(rtsp_source, width, height)
            except TimeoutError:
                frame = None
            if (
                self._removed
                or self.coordinator.shutting_down
                or (self._channel_key is not None and not self.available)
                or rtsp_source != self._build_rtsp()
            ):
                return None
            # Keep the original camera's RTSP preview behavior when configured.
            if frame or self._channel_key is None:
                return frame

        if self._removed or not self.available:
            return None
        channels = camera_channels(self.data)
        key = self._channel_key or next(
            (channel.key for channel in channels if channel.channel_no == 1), None
        )
        if key is None:
            return None
        sources = self.coordinator.channel_image_sources(self._serial, key)
        password = self.coordinator.credentials.key(self._serial)
        for source in sources:
            url, known_source, generation = source
            image_key = (key, channels, source, password)
            if image_key == self._channel_image_key:
                return self._channel_image
            result = await async_channel_image(
                self.hass,
                self.coordinator,
                self._serial,
                key,
                url,
                password,
                known_source=known_source,
                generation=generation,
            )
            if result is not None and not result.startswith(b"\xff\xd8\xff"):
                result = await self.hass.async_add_executor_job(_preview_jpeg, result)
            if (
                self._removed
                or not self.available
                or channels != camera_channels(self.data)
                or password != self.coordinator.credentials.key(self._serial)
                or sources != self.coordinator.channel_image_sources(self._serial, key)
            ):
                return None
            if result is not None:
                self._channel_image_key = image_key
                self._channel_image = result
                return result
        return None

    async def _async_rtsp_preview(
        self, source: str, width: int | None, height: int | None
    ) -> bytes | None:
        """Share a local frame grab without making it part of cloud session cleanup."""
        key = (source, width, height)
        if (task := self._preview_tasks.get(key)) is None:
            # Don't tie this to entry unload: FFmpeg already owns its timeout.
            task = self.hass.async_create_background_task(
                ffmpeg.async_get_image(self.hass, source, width=width, height=height),
                "EZVIZ RTSP preview",
            )
            self._preview_tasks[key] = task

            def finished(future: asyncio.Task[bytes | None]) -> None:
                self._preview_tasks.pop(key)
                if not future.cancelled():
                    future.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    @override
    async def stream_source(self) -> str | None:
        """Return a shareable source only for the existing RTSP route."""
        if self._cloud_relay is not None:
            return None
        _LOGGER.debug(
            "Configuring Camera %s with ip: %s rtsp port: %s path: %s",
            self._serial,
            self.data["local_ip"],
            self.data["local_rtsp_port"],
            self._rtsp_path,
        )
        return self._build_rtsp()

    async def async_perform_wake_device(self) -> None:
        """Wake/ping the camera using a lightweight API call."""
        try:
            await self.coordinator.async_execute(
                self.coordinator.ezviz_client.get_detection_sensibility, self._serial
            )
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Wake camera", err) from err

    async def async_capture_picture(self) -> ServiceResponse:
        """Take a new picture through the EZVIZ cloud.

        Works without RTSP and wakes battery devices from standby. The picture
        is shown by the camera's snapshot image entity.
        """
        if self._channel_key is not None and (self._removed or not self.available):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ptz_point_unavailable"
            )
        try:
            channel_no = capture_channel_number(self.data, self._channel_key)
            result = await self.coordinator.async_execute(
                self.coordinator.ezviz_client.capture_picture, self._serial, channel_no
            )
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Capture picture", err) from err

        capture_info = (
            result.get("captureInfo") if isinstance(result, Mapping) else None
        )
        pic_url = (
            capture_info.get("picUrl") if isinstance(capture_info, Mapping) else None
        )
        if not isinstance(pic_url, str) or not pic_url:
            raise HomeAssistantError("EZVIZ returned no picture url")
        try:
            parsed_url = urlsplit(pic_url)
            valid_url = (
                parsed_url.scheme in ("http", "https")
                and parsed_url.hostname is not None
                and parsed_url.port != 0
            )
        except ValueError as err:
            raise HomeAssistantError("EZVIZ returned an invalid picture URL") from err
        if not valid_url:
            raise HomeAssistantError("EZVIZ returned an invalid picture URL")
        if self._channel_key is not None and (self._removed or not self.available):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ptz_point_unavailable"
            )

        async_dispatcher_send(
            self.hass, capture_signal(self._serial, self._channel_key), pic_url
        )
        return {"picture_url": pic_url}

    async def async_ptz_control_coordinates(
        self,
        x_axis: float,
        y_axis: float,
        lens: str = "auto",
    ) -> None:
        """Send one image-plane point command without fabricating motor state."""
        if self._removed or not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ptz_point_unavailable"
            )
        bind_primary = (
            lens == "auto"
            and self._channel_key is None
            and (entry := self.coordinator.config_entry) is not None
            and bool(reported_lens_channels(self.coordinator, entry, self._serial))
        )
        resource, index = point_route(
            self.data, lens, channel_key=self._channel_key, bind_primary=bind_primary
        )
        try:
            accepted = await self.coordinator.async_execute(
                partial(
                    self.coordinator.ezviz_client.ptz_control_coordinates,
                    self._serial,
                    x_axis,
                    y_axis,
                    resource_identifier=resource,
                    local_index=index,
                )
            )
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Move camera to image position", err) from err
        if accepted is False:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ptz_point_rejected"
            )


class EzvizChannelCamera(EzvizCamera):
    """A channel-specific target sharing the physical camera's account/device."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        channel: CameraChannel,
    ) -> None:
        """Keep the existing camera ID and source separate from channel views."""
        super().__init__(hass, coordinator, serial, "", "", "", channel=channel)
        self._attr_unique_id = f"{serial}_channel_{channel.key}"
        self._attr_translation_key = "camera_channel"
        self._attr_translation_placeholders = {"channel": channel.local_index}

    @property
    @override
    def available(self) -> bool:
        """Require both device health and the exact reported channel identity."""
        return self._channel_key is not None and channel_available(
            self.coordinator, self._serial, self._channel_key
        )

    @property
    @override
    def is_recording(self) -> bool:
        """No lens-specific recording state is inferred from device alarm settings."""
        return False

    @property
    @override
    def motion_detection_enabled(self) -> bool:
        """Shared defence is not a reported per-lens setting."""
        return False

    @override
    async def async_enable_motion_detection(self) -> None:
        """Use the existing physical-device target for shared defence settings."""
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="channel_setting_shared"
        )

    @override
    async def async_disable_motion_detection(self) -> None:
        """Do not turn off every lens through an apparently lens-scoped target."""
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="channel_setting_shared"
        )
