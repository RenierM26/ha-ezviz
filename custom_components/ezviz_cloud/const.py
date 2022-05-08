"""Constants for the ezviz integration."""

DOMAIN = "ezviz_cloud"
MANUFACTURER = "Ezviz"

# Configuration
ATTR_SERIAL = "serial"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"
ATTR_HOME = "HOME_MODE"
ATTR_AWAY = "AWAY_MODE"
ATTR_SLEEP = "SLEEP_MODE"
ATTR_TYPE_CLOUD = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA = "CAMERA_ACCOUNT"
CONF_SESSION_ID = "session_id"
CONF_RFSESSION_ID = "rf_session_id"
CONF_EZVIZ_ACCOUNT = "ezviz_account"

# Services data
DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
ATTR_ENABLE = "enable"
ATTR_DIRECTION = "direction"
ATTR_SPEED = "speed"
ATTR_LEVEL = "level"
ATTR_TYPE = "type_value"

# Service names
SERVICE_PTZ = "ptz"
SERVICE_ALARM_TRIGGER = "sound_alarm"
SERVICE_WAKE_DEVICE = "wake_device"
SERVICE_ALARM_SOUND = "alarm_sound"
SERVICE_DETECTION_SENSITIVITY = "set_alarm_detection_sensibility"

# Defaults
EU_URL = "apiieu.ezvizlife.com"
RUSSIA_URL = "apirus.ezvizru.com"
DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_RTSP_PORT = 554
DEFAULT_TIMEOUT = 25
DEFAULT_FFMPEG_ARGUMENTS = ""

# Data
DATA_COORDINATOR = "coordinator"
