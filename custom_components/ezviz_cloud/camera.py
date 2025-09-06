"""Support EZVIZ camera devices (cloud-only model).

This module exposes Home Assistant camera entities based on data provided by the
cloud-scoped EZVIZ config entry. There are no per-camera config entries or discovery
flows. Per-camera configuration is read from the cloud entry's options at:

    entry.options["cameras"][<SERIAL>]

Where <SERIAL> is the canonical (uppercased, stripped) camera serial. The legacy
option name `CONF_FFMPEG_ARGUMENTS` is used to store the RTSP *path* (e.g.,
"/Streaming/Channels/101"). We keep this name for compatibility with existing setups.
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
    SERVICE_WAKE_DEVICE,
)
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)

OPTIONS_KEY_CAMERAS = "cameras"


def _norm_serial(value: Any) -> str:
    """Return the canonical camera serial key (uppercased, stripped).

    Parameters
    ----------
    value:
        Raw serial value from coordinator data or options map.

    Returns:
    -------
    str
        Canonicalized serial.
    """
    return str(value).strip().upper()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EZVIZ cameras for a cloud config entry.

    This wires camera entities directly from the coordinator's in-memory device map
    and the cloud entry's per-camera options. No discovery or per-camera entries.

    Parameters
    ----------
    hass:
        Home Assistant instance.
    entry:
        The cloud-scoped EZVIZ config entry.
    async_add_entities:
        Callback to register entities with Home Assistant.
    """
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    # Normalize all per-camera option keys once for robust lookups.
    cams_opts_raw: Mapping[str, dict[str, Any]] = entry.options.get(
        OPTIONS_KEY_CAMERAS, {}
    )
    cams_opts: dict[str, dict[str, Any]] = {
        _norm_serial(k): v for k, v in dict(cams_opts_raw).items()
    }

    entities: list[EzvizCamera] = []
    for serial in coordinator.data:
        norm_serial = _norm_serial(serial)
        per_cam: dict[str, Any] = cams_opts.get(norm_serial, {})

        username: str = per_cam.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME)

        use_vc: bool = bool(per_cam.get(CONF_RTSP_USES_VERIFICATION_CODE, False))
        enc_key: str | None = per_cam.get(CONF_ENC_KEY)
        password: str = (enc_key if use_vc else per_cam.get(CONF_PASSWORD)) or ""

        rtsp_path: str = (
            per_cam.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS) or ""
        )
        if rtsp_path and not rtsp_path.startswith("/"):
            # Be lenient: if users saved path without leading slash, add it.
            rtsp_path = "/" + rtsp_path

        if not password:
            _LOGGER.warning(
                "Camera %s missing RTSP password%s; stream may be unavailable until provided",
                norm_serial,
                " (verification code expected in enc key)" if use_vc else "",
            )

        entities.append(
            EzvizCamera(
                hass=hass,
                coordinator=coordinator,
                serial=norm_serial,  # canonical unique_id
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
    """EZVIZ security camera entity.

    The entity builds an RTSP URL from coordinator data (`local_ip`, `local_rtsp_port`)
    and per-camera credentials/path taken from the cloud config entry options.
    """

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
        """Initialize the camera entity.

        Parameters
        ----------
        hass:
            Home Assistant instance.
        coordinator:
            EZVIZ data coordinator (provides `.data` and `.ezviz_client`).
        serial:
            Canonical camera serial used as unique_id.
        camera_username:
            Username for RTSP auth (often 'admin' or device user).
        camera_password:
            Password (or verification code when `CONF_RTSP_USES_VERIFICATION_CODE` is True).
        rtsp_path:
            RTSP path segment (e.g., '/Streaming/Channels/101').
        """
        super().__init__(coordinator, serial)
        Camera.__init__(self)

        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True

        self._username: str = camera_username
        self._password: str = camera_password
        self._rtsp_path: str = rtsp_path
        self._ffmpeg = get_ffmpeg_manager(hass)
        self._client = self.coordinator.ezviz_client

        # unique_id = canonical serial
        self._attr_unique_id = serial

        # initial cache; rebuilt on demand
        self._rtsp_stream: str = self._build_rtsp()

    # ----- helpers -----

    def _build_rtsp(self) -> str:
        """Build an RTSP URL from coordinator data and per-camera credentials.

        Returns:
        -------
        str
            RTSP URL in the form:
            'rtsp://<user>:<pass>@<ip>:<port><path>'
        """
        ip = str(self.data.get("local_ip", "") or "")
        port = str(self.data.get("local_rtsp_port", "") or "")
        path = self._rtsp_path or ""
        return f"rtsp://{self._username}:{self._password}@{ip}:{port}{path}"

    # ----- HA camera entity surface -----

    @property
    def is_recording(self) -> bool:
        """Return True if the device is currently recording.

        Notes:
        -----
        This checks the `alarm_notify` flag as a proxy for defence/recording status,
        mirroring previous behavior. Adjust if a more precise field is available.
        """
        return bool(self.data.get("alarm_notify"))

    @property
    def motion_detection_enabled(self) -> bool:
        """Return True if motion detection is enabled."""
        return bool(self.data.get("alarm_notify"))

    def enable_motion_detection(self) -> None:
        """Enable motion detection (a.k.a. defence) on the device."""
        try:
            self._client().set_camera_defence(self._serial, 1)
        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    def disable_motion_detection(self) -> None:
        """Disable motion detection (a.k.a. defence) on the device."""
        try:
            self._client().set_camera_defence(self._serial, 0)
        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a single frame from the camera stream via ffmpeg.

        Parameters
        ----------
        width, height:
            Optional dimensions for the captured frame.

        Returns:
        -------
        Optional[bytes]
            Encoded image bytes, or None if unavailable.
        """
        return await ffmpeg.async_get_image(
            self.hass, self._build_rtsp(), width=width, height=height
        )

    async def stream_source(self) -> str:
        """Return the RTSP stream source for HA's stream component.

        Returns:
        -------
        str
            RTSP URL.
        """
        self._rtsp_stream = self._build_rtsp()
        _LOGGER.debug(
            "Configuring Camera %s with ip: %s rtsp port: %s path: %s",
            self._serial,
            self.data.get("local_ip"),
            self.data.get("local_rtsp_port"),
            self._rtsp_path,
        )
        return self._rtsp_stream

    def perform_wake_device(self) -> None:
        """Wake/ping the camera using a lightweight API call.

        Raises:
        ------
        PyEzvizError
            If the device cannot be contacted.
        """
        try:
            # Any light read is OK here; keep parity with previous behavior.
            self._client().get_detection_sensibility(self._serial)
        except (HTTPError, PyEzvizError) as err:
            raise PyEzvizError("Cannot wake device") from err
