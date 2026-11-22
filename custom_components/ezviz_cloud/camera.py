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
import logging
from typing import Any, override
from urllib.parse import quote, urlsplit

from pyezvizapi.devices.channels import (
    CameraChannel,
    capture_channel_number,
    resolve_camera_channel,
)
from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.image import infer_image_type
from homeassistant.components.stream import CONF_USE_WALLCLOCK_AS_TIMESTAMPS, Stream
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .channels import (
    async_add_channel_entities,
    capture_signal,
    channel_available,
    is_additional_camera_lens,
    lens_stream_path,
)
from .const import (
    CONF_ENC_KEY,
    CONF_RTSP_STREAM_PATH,
    CONF_RTSP_USES_VERIFICATION_CODE,
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

        rtsp_path: str | None = per_cam.get(
            CONF_RTSP_STREAM_PATH, DEFAULT_RTSP_STREAM_PATH
        )

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

        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True

        self._username: str = camera_username
        self._password: str = camera_password
        self._rtsp_path: str | None = rtsp_path
        self._ffmpeg = get_ffmpeg_manager(hass)
        self._attr_unique_id = serial
        self._channel_key = channel.key if channel is not None else None
        if channel is None:
            self._attr_name = None
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
        # OptionsFlowWithReload owns form changes; entry update listeners are
        # incompatible with it. Credential actions notify the coordinator after
        # committing, so they still update an existing stream without a reload.
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
                # HA 2026.10 re-adds this object on registry rename. Recreate
                # the stream with current preferences and the new state callback.
                self.stream = None
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
            CameraEntityFeature.STREAM if self._build_rtsp() else CameraEntityFeature(0)
        )

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a single frame from the camera stream via ffmpeg."""
        if not (source := self._build_rtsp()):
            return None
        return await ffmpeg.async_get_image(
            self.hass, source, width=width, height=height
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
        resource, index = point_route(self.data, lens, channel_key=self._channel_key)
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
        self._channel_image_key: tuple[Any, ...] | None = None
        self._channel_image: bytes | None = None

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

    @override
    async def async_create_stream(self) -> Stream | None:
        """Create only a currently available lens stream."""
        if not self.available or not self._build_rtsp():
            return None
        return await super().async_create_stream()

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Use this lens's configured stream, or its channel-indexed cloud image."""
        if not self.available:
            return None
        if self._build_rtsp():
            if frame := await super().async_camera_image(width, height):
                return frame
        key = self._channel_key
        if key is None:
            return None
        sources = self.coordinator.channel_image_sources(self._serial, key)
        password = self.coordinator.credentials.key(self._serial)
        for source in sources:
            url, known_source, generation = source
            image_key = (source, password)
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
            if (
                not self.available
                or password != self.coordinator.credentials.key(self._serial)
                or sources != self.coordinator.channel_image_sources(self._serial, key)
            ):
                return None
            if result is not None:
                self.content_type = infer_image_type(result) or "image/jpeg"
                self._channel_image_key = image_key
                self._channel_image = result
                return result
        return None
