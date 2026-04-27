"""Support EZVIZ camera devices.

This module exposes Home Assistant camera entities based on data provided by the
cloud-scoped EZVIZ config entry. There are no per-camera config entries or discovery
flows. Per-camera configuration is read from the cloud entry's options at:

    entry.options["cameras"][<SERIAL>]

Where <SERIAL> is the camera serial. The legacy
option name `CONF_FFMPEG_ARGUMENTS` is used to store the RTSP *path* (e.g.,
"/Streaming/Channels/102"). We keep this name for compatibility with existing setups.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import CONF_USE_WALLCLOCK_AS_TIMESTAMPS
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)

from .const import (
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,  # used here as RTSP path (main/sub)
    CONF_RTSP_USES_VERIFICATION_CODE,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,  # default RTSP path
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
    SERVICE_WAKE_DEVICE,
)
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .utility import is_camera_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EZVIZ cameras for a cloud config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    cams_opts: Mapping[str, dict[str, Any]] = entry.options[OPTIONS_KEY_CAMERAS]

    entities: list[EzvizCamera] = []
    for serial, camera_data in coordinator.data.items():
        if not is_camera_device(camera_data):
            continue
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

    # Expose wake service on the camera platform.
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_WAKE_DEVICE, None, "perform_wake_device"
    )


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
        self._rtsp_stream: str = self._build_rtsp()

    def _build_rtsp(self) -> str:
        """Build an RTSP URL from coordinator data and per-camera credentials.

        Returns:
            str
            RTSP URL in the form:'rtsp://<user>:<pass>@<ip>:<port><path>'
        """
        ip = self.data["local_ip"]
        port = self.data["local_rtsp_port"]
        path = self._rtsp_path or ""
        return f"rtsp://{self._username}:{self._password}@{ip}:{port}{path}"

    @property
    def is_recording(self) -> bool:
        """Return True if the device is currently recording."""
        return bool(self.data["alarm_notify"])

    @property
    def motion_detection_enabled(self) -> bool:
        """Return True if motion detection is enabled."""
        return bool(self.data["alarm_notify"])

    def enable_motion_detection(self) -> None:
        """Enable motion detection (a.k.a. defence) on the device."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 1)

        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    def disable_motion_detection(self) -> None:
        """Disable motion detection (a.k.a. defence) on the device."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 0)

        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a single frame from the camera stream via ffmpeg."""
        return await ffmpeg.async_get_image(
            self.hass, self._build_rtsp(), width=width, height=height
        )

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

    def perform_wake_device(self) -> None:
        """Wake/ping the camera using a lightweight API call."""
        try:
            self.coordinator.ezviz_client.get_detection_sensibility(self._serial)
        except (HTTPError, PyEzvizError) as err:
            raise PyEzvizError("Cannot wake device") from err
