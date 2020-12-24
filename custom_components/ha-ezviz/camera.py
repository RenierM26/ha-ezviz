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
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import config_validation as cv

_LOGGER = logging.getLogger(__name__)

CONF_CAMERAS = "cameras"

DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_RTSP_PORT = "554"
DOMAIN = "ha-ezviz"
DATA_FFMPEG = "ffmpeg"

EZVIZ_DATA = "ezviz"
ENTITIES = "entities"

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

def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Ezviz IP Cameras."""

    conf_cameras = config[CONF_CAMERAS]

    account = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]

    try:
        ezviz_client = EzvizClient(account, password)
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

#        camera["ezviz_camera"] = EzvizCamera(ezviz_client, camera_serial)

        camera_entities.append(HassEzvizCamera(**camera))

    add_entities(camera_entities)

    """Setup Services"""
    def ezviz_get_detection_sensibility(call):
        """Basicaly queries device to wake."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))

        ezviz_client.get_detection_sensibility(device_serial)

    def ezviz_switch_state(call):
        """Set camera status led service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.LIGHT.value, device_cmd)

    def ezviz_switch_audio(call):
        """Set camera audio service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.SOUND.value, device_cmd)

    def ezviz_switch_ir(call):
        """Set camera ir led service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.INFRARED_LIGHT, device_cmd)

    def ezviz_switch_privacy(call):
        """Set camera privacy service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.PRIVACY.value, device_cmd)

    def ezviz_switch_sleep(call):
        """Set camera sleep service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.SLEEP.value, device_cmd)

    def ezviz_switch_follow_move(call):
        """Set camera enable follow movement service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.switch_status(device_serial, DeviceSwitchType.MOBILE_TRACKING.value, device_cmd)

    def ezviz_alarm_notify(call):
        """Enable/Disable alarm notification service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.data_report(device_serial, device_cmd)

    def ezviz_alarm_sound(call):
        """Enable/Disable movement sound alarm."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.alarm_sound(device_serial, device_cmd, 1)

    def set_alarm_detection_sensibility(call):
        """Set camera detection sensibility level service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_cmd = str(data.pop('cmd'))

        ezviz_client.detection_sensibility(device_serial, device_cmd)

    def ezviz_ptz(call):
        """Set camera status led service."""
        data = dict(call.data)
        device_serial = str(data.pop('serial'))
        device_speed = str(data.pop('speed'))
        device_direction = str(data.pop('direction'))

        ezviz_client.ptzControl(str(device_direction).upper(), device_serial, 'START', device_speed)
        ezviz_client.ptzControl(str(device_direction).upper(), device_serial, 'STOP', device_speed)

    hass.services.register(DOMAIN, "ezviz_get_detection_sensibility", ezviz_get_detection_sensibility)
    hass.services.register(DOMAIN, "ezviz_switch_state", ezviz_switch_state)
    hass.services.register(DOMAIN, "ezviz_switch_audio", ezviz_switch_audio)
    hass.services.register(DOMAIN, "ezviz_switch_ir", ezviz_switch_ir)
    hass.services.register(DOMAIN, "ezviz_switch_privacy", ezviz_switch_privacy)
    hass.services.register(DOMAIN, "ezviz_switch_sleep", ezviz_switch_sleep)
    hass.services.register(DOMAIN, "ezviz_switch_follow_move", ezviz_switch_follow_move)
    hass.services.register(DOMAIN, "ezviz_ptz", ezviz_ptz)
    hass.services.register(DOMAIN, "ezviz_alarm_notify", ezviz_alarm_notify)
    hass.services.register(DOMAIN, "ezviz_alarm_sound", ezviz_alarm_sound)
    hass.services.register(DOMAIN, "set_alarm_detection_sensibility", set_alarm_detection_sensibility)

class HassEzvizCamera(Camera):
    """An implementation of a Foscam IP camera."""

    def __init__(self, **data):
        """Initialize an Ezviz camera."""
        super().__init__()

        self._username = data["username"]
        self._password = data["password"]
        self._rtsp_stream = data["rtsp_stream"]
        self._unique_id = data["serial"]
        self._ezviz_client = data["ezviz_client"]

#        self._ezviz_camera = data["ezviz_camera"]
        self._serial = data["serial"]
        self._name = data["name"]
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
        self._ffmpeg = None

    def update(self):
        """Update the camera states."""

        self._ezviz_client.login()
        self._ezviz_camera = EzvizCamera(self._ezviz_client, self._serial)
        data = self._ezviz_camera.status()

        self._name = data["name"]
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

    async def async_added_to_hass(self):
        """Subscribe to ffmpeg and add camera to list."""
        self._ffmpeg = self.hass.data[DATA_FFMPEG]

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
            # if true, if some movement is detected, the app is notified
            "alarm_notify": self._alarm_notify,
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
            # from 1 to 9, the higher is the sensibility, the more it will detect small movements
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
    def brand(self):
        """Return the camera brand."""
        return "Ezviz"

    @property
    def supported_features(self):
        """Return supported features."""
        if self._rtsp_stream:
            return SUPPORT_STREAM
        return 0

    @property
    def model(self):
        """Return the camera model."""
        return self._device_sub_category

    @property
    def is_on(self):
        """Return true if on."""
        return self._status

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    @property
    def unique_id(self):
        """Return the name of this camera."""
        return self._unique_id

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