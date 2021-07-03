"""Support ezviz camera devices."""
import asyncio
import logging

from haffmpeg.tools import IMAGE_JPEG, ImageFrame
from pyezviz.exceptions import HTTPError, InvalidHost, PyEzvizError
import voluptuous as vol

from homeassistant.components.camera import PLATFORM_SCHEMA, SUPPORT_STREAM, Camera
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.config_entries import SOURCE_DISCOVERY, SOURCE_IGNORE, SOURCE_IMPORT
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_DIRECTION,
    ATTR_ENABLE,
    ATTR_LEVEL,
    ATTR_SERIAL,
    ATTR_SPEED,
    ATTR_TYPE,
    CONF_CAMERAS,
    CONF_FFMPEG_ARGUMENTS,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_RTSP_PORT,
    DIR_DOWN,
    DIR_LEFT,
    DIR_RIGHT,
    DIR_UP,
    DOMAIN,
    MANUFACTURER,
    SERVICE_ALARM_SOUND,
    SERVICE_ALARM_TRIGER,
    SERVICE_DETECTION_SENSITIVITY,
    SERVICE_PTZ,
    SERVICE_WAKE_DEVICE,
)

CAMERA_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): cv.string, vol.Required(CONF_PASSWORD): cv.string}
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_CAMERAS, default={}): {cv.string: CAMERA_SCHEMA},
    }
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up a Ezviz IP Camera from platform config."""
    _LOGGER.warning(
        "Loading ezviz via platform config is deprecated, it will be automatically imported. Please remove it afterwards"
    )

    # Check if entry config exists and skips import if it does.
    if hass.config_entries.async_entries(DOMAIN):
        return

    # Check if importing camera account.
    if CONF_CAMERAS in config:
        cameras_conf = config[CONF_CAMERAS]
        for serial, camera in cameras_conf.items():
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_IMPORT},
                    data={
                        ATTR_SERIAL: serial,
                        CONF_USERNAME: camera[CONF_USERNAME],
                        CONF_PASSWORD: camera[CONF_PASSWORD],
                    },
                )
            )

    # Check if importing main ezviz cloud account.
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=config,
        )
    )


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Ezviz cameras based on a config entry."""

    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    camera_config_entries = hass.config_entries.async_entries(DOMAIN)

    camera_entities = []

    for idx, camera in enumerate(coordinator.data):

        # There seem to be a bug related to localRtspPort in Ezviz API...
        local_rtsp_port = DEFAULT_RTSP_PORT

        camera_rtsp_entry = [
            item
            for item in camera_config_entries
            if item.unique_id == camera[ATTR_SERIAL]
        ]

        if camera["local_rtsp_port"] != 0:
            local_rtsp_port = camera["local_rtsp_port"]

        if camera_rtsp_entry:
            conf_cameras = camera_rtsp_entry[0]

            # Skip ignored entities.
            if conf_cameras.source == SOURCE_IGNORE:
                continue

            ffmpeg_arguments = conf_cameras.options.get(
                CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
            )

            camera_username = conf_cameras.data[CONF_USERNAME]
            camera_password = conf_cameras.data[CONF_PASSWORD]

            camera_rtsp_stream = f"rtsp://{camera_username}:{camera_password}@{camera['local_ip']}:{local_rtsp_port}{ffmpeg_arguments}"
            _LOGGER.debug(
                "Camera %s source stream: %s", camera[ATTR_SERIAL], camera_rtsp_stream
            )

        else:

            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_DISCOVERY},
                    data={
                        ATTR_SERIAL: camera[ATTR_SERIAL],
                        CONF_IP_ADDRESS: camera["local_ip"],
                    },
                )
            )

            camera_username = DEFAULT_CAMERA_USERNAME
            camera_password = ""
            camera_rtsp_stream = ""
            ffmpeg_arguments = DEFAULT_FFMPEG_ARGUMENTS
            _LOGGER.warning(
                "Found camera with serial %s without configuration. Please go to integration to complete setup",
                camera[ATTR_SERIAL],
            )

        camera_entities.append(
            EzvizCamera(
                hass,
                coordinator,
                idx,
                camera_username,
                camera_password,
                camera_rtsp_stream,
                local_rtsp_port,
                ffmpeg_arguments,
            )
        )

    async_add_entities(camera_entities)

    platform = entity_platform.current_platform.get()

    platform.async_register_entity_service(
        SERVICE_PTZ,
        {
            vol.Required(ATTR_DIRECTION): vol.In(
                [DIR_UP, DIR_DOWN, DIR_LEFT, DIR_RIGHT]
            ),
            vol.Required(ATTR_SPEED): cv.positive_int,
        },
        "perform_ptz",
    )

    platform.async_register_entity_service(
        SERVICE_ALARM_TRIGER,
        {
            vol.Required(ATTR_ENABLE): cv.positive_int,
        },
        "perform_sound_alarm",
    )

    platform.async_register_entity_service(
        SERVICE_WAKE_DEVICE, {}, "perform_wake_device"
    )

    platform.async_register_entity_service(
        SERVICE_ALARM_SOUND,
        {vol.Required(ATTR_LEVEL): cv.positive_int},
        "perform_alarm_sound",
    )

    platform.async_register_entity_service(
        SERVICE_DETECTION_SENSITIVITY,
        {
            vol.Required(ATTR_LEVEL): cv.positive_int,
            vol.Required(ATTR_TYPE): cv.positive_int,
        },
        "perform_set_alarm_detection_sensibility",
    )


class EzvizCamera(CoordinatorEntity, Camera, RestoreEntity):
    """An implementation of a Ezviz security camera."""

    def __init__(
        self,
        hass,
        coordinator,
        idx,
        camera_username,
        camera_password,
        camera_rtsp_stream,
        local_rtsp_port,
        ffmpeg_arguments,
    ):
        """Initialize a Ezviz security camera."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self._username = camera_username
        self._password = camera_password
        self._rtsp_stream = camera_rtsp_stream
        self._idx = idx
        self._ffmpeg = hass.data[DATA_FFMPEG]
        self._local_rtsp_port = local_rtsp_port
        self._ffmpeg_arguments = ffmpeg_arguments

        self._serial = self.coordinator.data[self._idx]["serial"]
        self._name = self.coordinator.data[self._idx]["name"]
        self._local_ip = self.coordinator.data[self._idx]["local_ip"]

    @property
    def available(self):
        """Return True if entity is available."""
        if self.coordinator.data[self._idx]["status"] == 2:
            return False

        return True

    @property
    def supported_features(self):
        """Return supported features."""
        if self._rtsp_stream:
            return SUPPORT_STREAM
        return 0

    @property
    def name(self):
        """Return the name of this device."""
        return self._name

    @property
    def model(self):
        """Return the model of this device."""
        return self.coordinator.data[self._idx]["device_sub_category"]

    @property
    def brand(self):
        """Return the manufacturer of this device."""
        return MANUFACTURER

    @property
    def is_on(self):
        """Return true if on."""
        return bool(self.coordinator.data[self._idx]["status"])

    @property
    def is_recording(self):
        """Return true if the device is recording."""
        return self.coordinator.data[self._idx]["alarm_notify"]

    @property
    def motion_detection_enabled(self):
        """Camera Motion Detection Status."""
        return self.coordinator.data[self._idx]["alarm_notify"]

    def enable_motion_detection(self):
        """Enable motion detection in camera."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 1)

        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    def disable_motion_detection(self):
        """Disable motion detection."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 0)

        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    @property
    def unique_id(self):
        """Return the name of this camera."""
        return self._serial

    async def async_camera_image(self):
        """Return a frame from the camera stream."""
        ffmpeg = ImageFrame(self._ffmpeg.binary)

        image = await asyncio.shield(
            ffmpeg.get_image(self._rtsp_stream, output_format=IMAGE_JPEG)
        )
        return image

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(DOMAIN, self._serial)},
            "name": self.coordinator.data[self._idx]["name"],
            "model": self.coordinator.data[self._idx]["device_sub_category"],
            "manufacturer": MANUFACTURER,
            "sw_version": self.coordinator.data[self._idx]["version"],
        }

    async def stream_source(self):
        """Return the stream source."""
        local_ip = self.coordinator.data[self._idx]["local_ip"]
        if self._local_rtsp_port:
            rtsp_stream_source = (
                f"rtsp://{self._username}:{self._password}@"
                f"{local_ip}:{self._local_rtsp_port}{self._ffmpeg_arguments}"
            )
            _LOGGER.debug(
                "Camera %s source stream: %s", self._serial, rtsp_stream_source
            )
            self._rtsp_stream = rtsp_stream_source
            return rtsp_stream_source
        return None

    def perform_ptz(self, direction, speed):
        """Perform a PTZ action on the camera."""
        _LOGGER.debug("PTZ action '%s' on %s", direction, self._name)
        try:
            self.coordinator.ezviz_client.ptz_control(
                str(direction).upper(), self._serial, "START", speed
            )
            self.coordinator.ezviz_client.ptz_control(
                str(direction).upper(), self._serial, "STOP", speed
            )

        except HTTPError as err:
            raise HTTPError("Cannot perform PTZ") from err

    def perform_sound_alarm(self, enable):
        """Sound the alarm on a camera."""
        try:
            self.coordinator.ezviz_client.sound_alarm(self._serial, enable)
        except HTTPError as err:
            raise HTTPError("Cannot sound alarm") from err

    def perform_wake_device(self):
        """Basically wakes the camera by querying the device."""
        try:
            self.coordinator.ezviz_client.get_detection_sensibility(self._serial)
        except (HTTPError, PyEzvizError) as err:
            raise PyEzvizError("Cannot wake device") from err

    def perform_alarm_sound(self, level):
        """Enable/Disable movement sound alarm."""
        try:
            self.coordinator.ezviz_client.alarm_sound(self._serial, level, 1)
        except HTTPError as err:
            raise HTTPError(
                "Cannot set alarm sound level for on movement detected"
            ) from err

    def perform_set_alarm_detection_sensibility(self, level, type_value):
        """Set camera detection sensibility level service."""
        try:
            self.coordinator.ezviz_client.detection_sensibility(
                self._serial, level, type_value
            )
        except (HTTPError, PyEzvizError) as err:
            raise PyEzvizError("Cannot set detection sensitivity level") from err
