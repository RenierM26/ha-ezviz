"""Support EZVIZ camera devices.

This module exposes Home Assistant camera entities based on data provided by the
cloud-scoped EZVIZ config entry. There are no per-camera config entries or discovery
flows. Per-camera configuration is read from the cloud entry's options at:

    entry.options["cameras"][<SERIAL>]

Where <SERIAL> is the camera serial. The legacy
option name `CONF_FFMPEG_ARGUMENTS` is used to store the RTSP *path* (e.g.,
"/Streaming/Channels/102"). We keep this name for compatibility with existing setups.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import logging
from typing import Any, override
from urllib.parse import quote, urlsplit

from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import CONF_USE_WALLCLOCK_AS_TIMESTAMPS, Stream
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,  # used here as RTSP path (main/sub)
    CONF_RTSP_USES_VERIFICATION_CODE,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,  # default RTSP path
    OPTIONS_KEY_CAMERAS,
    SIGNAL_PICTURE_CAPTURED,
)
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .errors import request_error
from .runtime import EzvizConfigEntry

_LOGGER = logging.getLogger(__name__)


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

        rtsp_path: str = per_cam.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS)

        if rtsp_path and not rtsp_path.startswith("/"):
            # Be lenient: if users saved path without leading slash, add it.
            rtsp_path = "/" + rtsp_path

        if not password:
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


class EzvizCamera(EzvizEntity, Camera):
    """EZVIZ security camera entity."""

    _attr_name: str | None = None
    _attr_supported_features: CameraEntityFeature = CameraEntityFeature.STREAM

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        camera_username: str,
        camera_password: str,
        rtsp_path: str,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__(coordinator, serial)
        Camera.__init__(self)

        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True

        self._username: str = camera_username
        self._password: str = camera_password
        self._rtsp_path: str = rtsp_path
        self._ffmpeg = get_ffmpeg_manager(hass)
        self._attr_unique_id = serial
        self._stream_lifecycle_lock = asyncio.Lock()
        self._removed = False
        self._rtsp_stream: str = self._build_rtsp()

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
                    CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                )
        ip = self.data["local_ip"]
        port = self.data["local_rtsp_port"]
        path = self._rtsp_path or ""
        if path and not path.startswith("/"):
            path = "/" + path
        return f"rtsp://{quote(self._username, safe='')}:{quote(self._password, safe='')}@{ip}:{port}{path}"

    @override
    async def async_added_to_hass(self) -> None:
        """Watch committed options as well as coordinator updates."""
        await super().async_added_to_hass()
        if entry := self.coordinator.config_entry:
            self.async_on_remove(entry.add_update_listener(self._async_options_updated))
        self._refresh_stream_source()

    async def _async_options_updated(
        self, hass: HomeAssistant, entry: EzvizConfigEntry
    ) -> None:
        """Refresh only after HA has committed the new options."""
        self._refresh_stream_source()

    @callback
    def _refresh_stream_source(self) -> None:
        """Restart an existing stream only when its selected source changes."""
        if self._removed or self.coordinator.shutting_down:
            return
        source = self._build_rtsp()
        if source != self._rtsp_stream:
            self._rtsp_stream = source
            if self.stream is not None:
                self.stream.update_source(source)

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
        self._removed = True
        async with self._stream_lifecycle_lock:
            if self.stream is not None:
                self.stream.dynamic_stream_settings = replace(
                    self.stream.dynamic_stream_settings, preload_stream=False
                )
                await self.stream.stop()
        await super().async_will_remove_from_hass()

    @override
    async def async_create_stream(self) -> Stream | None:
        """Serialize stream creation with removal and refresh after creation awaits."""
        async with self._stream_lifecycle_lock:
            if self._removed or self.coordinator.shutting_down:
                return None
            stream = await super().async_create_stream()
            # Removal may have started while HA awaited stream settings.
            if self._removed or self.coordinator.shutting_down:
                return None
            # Options may have changed during the same await.
            if stream is not None:
                source = self._build_rtsp()
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
            await self.coordinator.async_execute(
                self.coordinator.ezviz_client.set_camera_defence, self._serial, 1
            )

        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    @override
    async def async_disable_motion_detection(self) -> None:
        """Disable motion detection (a.k.a. defence) on the device."""
        try:
            await self.coordinator.async_execute(
                self.coordinator.ezviz_client.set_camera_defence, self._serial, 0
            )

        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a single frame from the camera stream via ffmpeg."""
        return await ffmpeg.async_get_image(
            self.hass, self._build_rtsp(), width=width, height=height
        )

    @override
    async def stream_source(self) -> str:
        """Return the RTSP stream source for HA's stream component."""
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
        try:
            result = await self.coordinator.async_execute(
                self.coordinator.ezviz_client.capture_picture, self._serial, 1
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

        async_dispatcher_send(
            self.hass, SIGNAL_PICTURE_CAPTURED.format(self._serial), pic_url
        )
        return {"picture_url": pic_url}
