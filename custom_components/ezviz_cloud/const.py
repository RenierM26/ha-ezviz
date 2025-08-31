"""Constants for the EZVIZ integration."""

DOMAIN = "ezviz_cloud"
MANUFACTURER = "EZVIZ"

# Configuration
ATTR_SERIAL = "serial"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"
ATTR_TYPE_CLOUD = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA = "CAMERA_ACCOUNT"
CONF_SESSION_ID = "session_id"
CONF_RF_SESSION_ID = "rf_session_id"
CONF_EZVIZ_ACCOUNT = "ezviz_account"
CONF_ENC_KEY = "enc_key"
CONF_TEST_RTSP_CREDENTIALS = "test_rtsp_credentials"
CONF_RTSP_USES_VERIFICATION_CODE = "rtsp_uses_verification_code"
CONF_CAM_VERIFICATION_2FA_CODE = "cam_verification_2fa_code"
CONF_CAM_ENC_2FA_CODE = "cam_encryption_2fa_code"
CONF_USER_ID = "user_id"

# Service names
SERVICE_WAKE_DEVICE = "wake_device"

# Defaults
EU_URL = "apiieu.ezvizlife.com"
RUSSIA_URL = "apirus.ezvizru.com"
DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_TIMEOUT = 25
DEFAULT_FFMPEG_ARGUMENTS = "/Streaming/Channels/102"

# Data
DATA_COORDINATOR = "coordinator"
