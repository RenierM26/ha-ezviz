"""This component provides basic support for Ezviz IP cameras."""
import asyncio
import logging

# pylint: disable=import-error
from haffmpeg.tools import IMAGE_JPEG, ImageFrame
from pyezviz.camera import EzvizCamera
from pyezviz.client import EzvizClient, PyEzvizError
from pyezviz.DeviceSwitchType import DeviceSwitchType
import voluptuous as vol

from homeassistant.components.camera import PLATFORM_SCHEMA, SUPPORT_STREAM, Camera
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_REGION
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CAMERAS,
    ATTR_SERIAL,
    ATTR_SWITCH,
    ATTR_ENABLE,
    ATTR_DIRECTION,
    ATTR_SPEED,
    DEFAULT_REGION,
    MANUFACTURER,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_RTSP_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"

ATTR_LIGHT = "LIGHT"
ATTR_SOUND = "SOUND"
ATTR_INFRARED_LIGHT = "INFRARED_LIGHT"
ATTR_PRIVACY = "PRIVACY"
ATTR_SLEEP = "SLEEP"
ATTR_MOBILE_TRACKING = "MOBILE_TRACKING" 

CAMERA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string, 
        vol.Required(CONF_PASSWORD): cv.string
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): cv.string,
        vol.Optional(ATTR_CAMERAS, default={}): {cv.string: CAMERA_SCHEMA}
    }
)

SERVICE_SET_SWITCH_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERIAL): cv.string,
        vol.Required(ATTR_SWITCH): vol.In(
            [
                ATTR_LIGHT,
                ATTR_SOUND,
                ATTR_INFRARED_LIGHT,
                ATTR_PRIVACY,
                ATTR_SLEEP,
                ATTR_MOBILE_TRACKING
            ]
        ),
        vol.Optional(ATTR_ENABLE): cv.positive_int
    }
)

SERVICE_PTZ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERIAL): cv.string,
        vol.Required(ATTR_DIRECTION): vol.In(
            [
                DIR_UP,
                DIR_DOWN,
                DIR_LEFT,
                DIR_RIGHT
            ]
        ),
        vol.Required(ATTR_SPEED): cv.positive_int
    }
)

def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Ezviz IP Cameras."""

    conf_cameras = config[ATTR_CAMERAS]

    account = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]
    region = config[CONF_REGION]

    try:
        ezviz_client = EzvizClient(account, password, region)
        ezviz_client.login()
        cameras = ezviz_client.load_cameras()

    except PyEzvizError as exp:
        _LOGGER.error(exp)
        return

    # now, let's build the HASS devices
    camera_entities = []

    # Add the cameras as devices in HASS
    for camera in cameras:

        camera_username = DEFAULT_CAMERA_USERNAME
        camera_password = ""
        camera_rtsp_stream = ""
        camera_serial = camera["serial"]

        # There seem to be a bug related to localRtspPort in Ezviz API...
        local_rtsp_port = DEFAULT_RTSP_PORT
        if camera["local_rtsp_port"] and camera["local_rtsp_port"] != 0:
            local_rtsp_port = camera["local_rtsp_port"]

        if camera_serial in conf_cameras:
            camera_username = conf_cameras[camera_serial][CONF_USERNAME]
            camera_password = conf_cameras[camera_serial][CONF_PASSWORD]
            camera_rtsp_stream = f"rtsp://{camera_username}:{camera_password}@{camera['local_ip']}:{local_rtsp_port}"
            _LOGGER.debug(
                "Camera %s source stream: %s", camera["serial"], camera_rtsp_stream
            )

        else:
            _LOGGER.info(
                "Found camera with serial %s without configuration. Add it to configuration.yaml to see the camera stream",
                camera_serial,
            )

        camera["username"] = camera_username
        camera["password"] = camera_password
        camera["rtsp_stream"] = camera_rtsp_stream
        camera["ezviz_client"] = ezviz_client

        camera["ezviz_camera"] = EzvizCamera(ezviz_client, camera_serial)

        camera_entities.append(HassEzvizCamera(hass, **camera))

    add_entities(camera_entities)

    """Setup Services"""
    def ezviz_wake_device(service):
        """Basicaly queries device to wake."""
        ezviz_client.get_detection_sensibility(str(service.data['serial']))

    def ezviz_switch_set(service):
        """Set camera switch service."""
        service_switch = getattr(DeviceSwitchType, service.data[ATTR_SWITCH])
        
        ezviz_client.switch_status(service.data[ATTR_SERIAL], service_switch.value, service.data[ATTR_ENABLE])

    def ezviz_alarm_sound(service):
        """Enable/Disable movement sound alarm."""
        ezviz_client.alarm_sound(str(service.data[ATTR_SERIAL]), int(service.data['level']), 1)

    def ezviz_set_alarm_detection_sensibility(service):
        """Set camera detection sensibility level service."""
        ezviz_client.detection_sensibility(str(service.data[ATTR_SERIAL]), int(service.data['level']), int(service.data['type']))

    def ezviz_ptz(service):
        """Camera PTZ service."""
        ezviz_client.ptzControl(str(service.data[ATTR_DIRECTION]).upper(), service.data[ATTR_SERIAL], 'START', service.data[ATTR_SPEED])
        ezviz_client.ptzControl(str(service.data[ATTR_DIRECTION]).upper(), service.data[ATTR_SERIAL], 'STOP', service.data[ATTR_SPEED])

    hass.services.register(DOMAIN, "ezviz_wake_device", ezviz_wake_device)
    hass.services.register(DOMAIN, "ezviz_switch_set", ezviz_switch_set, schema=SERVICE_SET_SWITCH_SCHEMA)
    hass.services.register(DOMAIN, "ezviz_ptz", ezviz_ptz, SERVICE_PTZ_SCHEMA)
    hass.services.register(DOMAIN, "ezviz_alarm_sound", ezviz_alarm_sound)
    hass.services.register(DOMAIN, "ezviz_set_alarm_detection_sensibility", ezviz_set_alarm_detection_sensibility)

class HassEzvizCamera(Camera):
    """An implementation of a Foscam IP camera."""

    def __init__(self, hass, **data):
        """Initialize an Ezviz camera."""
        super().__init__()

        self._username = data["username"]
        self._password = data["password"]
        self._rtsp_stream = data["rtsp_stream"]
        self._ezviz_client = data["ezviz_client"]

        self._ezviz_camera = data["ezviz_camera"]
        self._serial = data["serial"]
        self._name = data["name"]
        self._version = data["version"]
        self._upgrade_available = data["upgrade_available"]
        self._status = data["status"]
        self._privacy = data["privacy"]
        self._sleep = data["sleep"]
        self._audio = data["audio"]
        self._ir_led = data["ir_led"]
        self._state_led = data["state_led"]
        self._follow_move = data["follow_move"]
        self._alarm_notify = data["alarm_notify"]
        self._alarm_schedules_enabled = data["alarm_schedules_enabled"]
        self._alarm_sound_mod = data["alarm_sound_mod"]
        self._encrypted = data["encrypted"]
        self._local_ip = data["local_ip"]
        self._battery_level = data["battery_level"]
        self._PIR_Status = data["PIR_Status"]    
        self._detection_sensibility = data["detection_sensibility"]
        self._device_sub_category = data["device_sub_category"]
        self._local_rtsp_port = data["local_rtsp_port"]
        self._last_alarm_time = data["last_alarm_time"]
        self._last_alarm_pic = data["last_alarm_pic"]
        self._ffmpeg = hass.data[DATA_FFMPEG]

    def update(self):
        """Update the camera states."""
        self._ezviz_client.login()
        data = self._ezviz_camera.status()

        self._name = data["name"]
        self._version = data["version"]
        self._upgrade_available = data["upgrade_available"]
        self._status = data["status"]
        self._privacy = data["privacy"]
        self._sleep = data["sleep"]
        self._audio = data["audio"]
        self._ir_led = data["ir_led"]
        self._state_led = data["state_led"]
        self._follow_move = data["follow_move"]
        self._alarm_notify = data["alarm_notify"]
        self._alarm_schedules_enabled = data["alarm_schedules_enabled"]
        self._alarm_sound_mod = data["alarm_sound_mod"]
        self._encrypted = data["encrypted"]
        self._local_ip = data["local_ip"]
        self._battery_level = data["battery_level"]
        self._PIR_Status = data["PIR_Status"]  
        self._detection_sensibility = data["detection_sensibility"]
        self._device_sub_category = data["device_sub_category"]
        self._local_rtsp_port = data["local_rtsp_port"]
        self._last_alarm_time = data["last_alarm_time"]
        self._last_alarm_pic = data["last_alarm_pic"]

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return True

    @property
    def device_state_attributes(self):
        """Return the Ezviz-specific camera state attributes."""
        return {
            #Camera firmware version
            "sw_version" : self._version,
            #Camera firmware version update available?
            "upgrade_available" : self._upgrade_available,
            # if privacy == true, the device closed the lid or did a 180° tilt
            "privacy": self._privacy,
            # if sleep == true, the device is sleeping?
            "sleep": self._sleep,
            # is the camera listening ?
            "audio": self._audio,
            # infrared led on ?
            "ir_led": self._ir_led,
            # state led on  ?
            "state_led": self._state_led,
            # if true, the camera will move automatically to follow movements
            "follow_move": self._follow_move,
            # if true, notification schedule(s) are configured
            "alarm_schedules_enabled": self._alarm_schedules_enabled,
            # if true, if some movement is detected, the camera makes some sound
            "alarm_sound_mod": self._alarm_sound_mod,
            # are the camera's stored videos/images encrypted?
            "encrypted": self._encrypted,
            # camera's local ip on local network
            "local_ip": self._local_ip,
            # camera's battery level if battery camera
            "battery_level": self._battery_level,
            # PIR sensor of camera. 0=open ir, and 1=closed ir
            "PIR_Status" : self._PIR_Status,  
            # from 1 to 6 or 1-100, the higher is the sensibility, the more it will detect small movements
            "detection_sensibility": self._detection_sensibility,
            # last alarm trigger date and time
            "Last alarm triggered" : self._last_alarm_time,
            # image of last event that triggered alarm
            "Last alarm image url": self._last_alarm_pic,
        }

    @property
    def available(self):
        """Return True if entity is available."""
        return self._status

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
        return self._device_sub_category

    @property
    def manufacturer(self):
        """Return the manufacturer of this device."""
        return MANUFACTURER

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(DOMAIN, self._serial)},
            "name": self._name,
            "model": self._device_sub_category,
            "manufacturer": MANUFACTURER,
            "sw_version" : self._version
        }

    @property
    def is_on(self):
        """Return true if on."""
        return self._status

    @property
    def motion_detection_enabled(self):
        """Camera Motion Detection Status."""
        return self._alarm_notify

    def enable_motion_detection(self):
        """Enable motion detection in camera."""
        try:
            ret = self._ezviz_client.data_report(self._serial, 1)
            if ret != True:
                return

            self._alarm_notify = True
        except TypeError:
            _LOGGER.debug("Communication problem")

    def disable_motion_detection(self):
        """Disable motion detection."""
        try:
            ret = self._ezviz_client.data_report(self._serial, 0)
            if ret != True:
                return

            self._alarm_notify = False
        except TypeError:
            _LOGGER.debug("Communication problem")

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

    async def stream_source(self):
        """Return the stream source."""
        if self._local_rtsp_port:
            rtsp_stream_source = (
                f"rtsp://{self._username}:{self._password}@"
                f"{self._local_ip}:{self._local_rtsp_port}"
            )
            _LOGGER.debug(
                "Camera %s source stream: %s", self._serial, rtsp_stream_source
            )
            self._rtsp_stream = rtsp_stream_source
            return rtsp_stream_source
        return None