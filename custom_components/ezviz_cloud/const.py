"""Constants for the EZVIZ Cloud integration."""

from typing import Final

DOMAIN: Final = "ezviz_cloud"
MANUFACTURER: Final = "EZVIZ"

# ---------------------------
# Entry/data typing
# ---------------------------

ATTR_TYPE_CLOUD: Final = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA: Final = "CAMERA_ACCOUNT"
ATTR_SERIAL: Final = "serial"

# ---------------------------
# Config flow / data keys
# ---------------------------

CONF_SESSION_ID: Final = "session_id"
CONF_RF_SESSION_ID: Final = "rf_session_id"
CONF_USER_ID: Final = "user_id"
CONF_EZVIZ_ACCOUNT: Final = "ezviz_account"

CONF_ENC_KEY: Final = "enc_key"
CONF_TEST_RTSP_CREDENTIALS: Final = "test_rtsp_credentials"
CONF_RTSP_USES_VERIFICATION_CODE: Final = "rtsp_uses_verification_code"

# Optional 2FA verification codes for login
CONF_CAM_VERIFICATION_2FA_CODE: Final = "cam_verification_2fa_code"
CONF_CAM_ENC_2FA_CODE: Final = "cam_encryption_2fa_code"

# Legacy naming retained — this field is actually the RTSP *path*
# e.g. "/Streaming/Channels/101" (main stream) or "/Streaming/Channels/102" (sub stream).
# It is NOT passed as ffmpeg CLI arguments.
CONF_FFMPEG_ARGUMENTS: Final = "ffmpeg_arguments"

# Region handling
CONF_REGION: Final = "region"
REGION_EU: Final = "eu"
REGION_RU: Final = "ru"
REGION_CUSTOM: Final = "custom"

EU_URL: Final = "apiieu.ezvizlife.com"
RUSSIA_URL: Final = "apirus.ezvizru.com"

# Mapping used by config_flow to resolve API hostnames
REGION_URLS: Final = {
    REGION_EU: EU_URL,
    REGION_RU: RUSSIA_URL,
}

# ---------------------------
# Defaults
# ---------------------------

DEFAULT_CAMERA_USERNAME: Final = "admin"
DEFAULT_TIMEOUT: Final = 25
DEFAULT_FFMPEG_ARGUMENTS: Final = "/Streaming/Channels/102"  # substream = safer default
DEFAULT_FETCH_MY_KEY: Final = "fetch_my_key"

# ---------------------------
# Services
# ---------------------------

SERVICE_WAKE_DEVICE: Final = "wake_device"

# ---------------------------
# hass.data keys
# ---------------------------

DATA_COORDINATOR: Final = "coordinator"
MQTT_HANDLER: Final = "mqtt_handler"
OPTIONS_KEY_CAMERAS: Final = "cameras"
