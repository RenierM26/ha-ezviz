"""Camera-options credential retrieval and explicit RTSP checks (no option commits)."""

import logging
from typing import TYPE_CHECKING, Any, cast

from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    PyEzvizError,
)
from pyezvizapi.test_cam_rtsp import TestRTSPAuth

from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME

from .const import (
    ATTR_SERIAL,
    CONF_CAM_ENC_2FA_CODE,
    CONF_CAM_VERIFICATION_2FA_CODE,
    CONF_ENC_KEY,
    CONF_RTSP_USES_VERIFICATION_CODE,
    DEFAULT_FETCH_MY_KEY,
)

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class CameraCredentialFetchError(PyEzvizError):
    """Identify the failed cloud credential request without exposing its response."""

    def __init__(self, field: str, data: dict, saved: Any) -> None:
        """Retain partial results for explicit review, never commit them on failure."""
        super().__init__("EZVIZ camera credential retrieval failed")
        self.field = field
        self.error_key = (
            "verification_code_fetch_failed"
            if field == CONF_PASSWORD
            else "encryption_key_fetch_failed"
        )
        self.data = dict(data)
        self.data.pop(CONF_CAM_VERIFICATION_2FA_CODE, None)
        self.data.pop(CONF_CAM_ENC_2FA_CODE, None)
        if isinstance(saved, str) and saved not in ("", DEFAULT_FETCH_MY_KEY):
            self.data[field] = saved


def _get_cam_verification_code(
    data: dict, ezviz_client: EzvizClient, verification_code: str | None = None
) -> Any:
    """Fetch camera verification/sticker code. May require one-time 2FA."""
    try:
        return ezviz_client.get_cam_auth_code(
            data[ATTR_SERIAL],
            msg_auth_code=verification_code,
            sender_type=0 if verification_code else 3,
        )

    except EzvizAuthVerificationCode as err:
        ezviz_client.get_2fa_check_code(
            username=data["cloud_account_username"], biz_type="DEVICE_AUTH_CODE"
        )
        raise EzvizAuthVerificationCode from err


def _get_cam_enc_key(
    data: dict, ezviz_client: EzvizClient, enc_2fa_code: str | None = None
) -> Any:
    """Fetch camera encryption key. May require one-time 2FA."""
    return ezviz_client.get_cam_key(
        data[ATTR_SERIAL],
        smscode=enc_2fa_code,
    )


def _test_camera_rtsp_creds(data: dict) -> None:
    """Attempt RTSP DESCRIBE using either verification code or enc key."""
    if data[CONF_RTSP_USES_VERIFICATION_CODE]:
        TestRTSPAuth(
            data[CONF_IP_ADDRESS], data[CONF_USERNAME], data[CONF_PASSWORD]
        ).main()
    else:
        TestRTSPAuth(
            data[CONF_IP_ADDRESS], data[CONF_USERNAME], data[CONF_ENC_KEY]
        ).main()


def _wake_camera(data: dict, ezviz_client: EzvizClient) -> None:
    """Wake a hibernating camera and immediately run an RTSP DESCRIBE test."""
    ezviz_client.get_detection_sensibility(data[ATTR_SERIAL])  # safe 'ping'
    _test_camera_rtsp_creds(data)


def _infer_supports_rtsp_from_category(cam_info: dict) -> bool:
    """Heuristic: most battery categories lack RTSP; some do support it though."""
    cat = cam_info["device_category"]
    return DeviceCatagories.BATTERY_CAMERA_DEVICE_CATEGORY.value not in cat


def _credential_fetch_error(
    field: str, data: dict, baseline: dict[str, Any], serial: str
) -> CameraCredentialFetchError:
    label = "verification/sticker code" if field == CONF_PASSWORD else "encryption key"
    _LOGGER.warning(
        "EZVIZ %s retrieval failed for camera %s; saved credentials are unchanged",
        label,
        serial,
    )
    return CameraCredentialFetchError(field, data, baseline.get(field))


async def async_resolve_camera_credentials(
    coordinator: EzvizDataUpdateCoordinator,
    data: dict,
    baseline: dict[str, Any],
    serial: str,
) -> dict:
    """Resolve/fetch creds; run RTSP wake+probe only when explicitly requested.

    - Fetches ENC key / verification code when the user typed 'fetch_my_key'.
    - Accepts optional one-time 2FA codes (never persisted).
    - If 'ephemeral_test_rtsp' is True and the camera likely supports RTSP,
      calls _wake_camera(), which in turn calls _test_camera_rtsp_creds().
    - Drops any one-time codes / flags from the returned dict.
    """
    ezviz_client: EzvizClient = coordinator.ezviz_client

    fetch_field: str | None = None
    try:
        # ENC key (used by newer cams & for last motion images)
        if data.get(CONF_ENC_KEY) == DEFAULT_FETCH_MY_KEY:
            fetch_field = CONF_ENC_KEY
            data[CONF_ENC_KEY] = await coordinator.async_execute(
                _get_cam_enc_key,
                data,
                ezviz_client,
                data.get(CONF_CAM_ENC_2FA_CODE),  # optional one-time MFA
            )
            fetch_field = None
            _LOGGER.info("Fetched encryption key for camera %s", data[ATTR_SERIAL])

        # Verification (sticker) code (older cam RTSP auth)
        if data.get(CONF_PASSWORD) == DEFAULT_FETCH_MY_KEY:
            fetch_field = CONF_PASSWORD
            data[CONF_PASSWORD] = await coordinator.async_execute(
                _get_cam_verification_code,
                data,
                ezviz_client,
                data.get(CONF_CAM_VERIFICATION_2FA_CODE),  # optional one-time MFA
            )
            fetch_field = None
            _LOGGER.info("Fetched verification code for camera %s", data[ATTR_SERIAL])

        # Optional one-time RTSP test: only if user requested
        if data.get("ephemeral_test_rtsp"):
            await coordinator.async_execute(_wake_camera, data, ezviz_client)
            _LOGGER.debug("RTSP credentials verified for camera %s", data[ATTR_SERIAL])

    except EzvizAuthVerificationCode:
        _LOGGER.warning(
            "EZVIZ requested 2FA code while preparing/testing %s",
            data.get(ATTR_SERIAL),
        )
        raise

    except DeviceException as err:
        if fetch_field is not None:
            raise _credential_fetch_error(fetch_field, data, baseline, serial) from err
        _LOGGER.warning(
            "Device error while preparing/testing %s", data.get(ATTR_SERIAL)
        )
        # Attach whatever data we have so far for prefill
        device_error = DeviceException(f"EZVIZ Device error: {err}")
        cast(Any, device_error).data = data
        raise device_error from err

    except AuthTestResultFailed as err:
        if fetch_field is not None:
            raise _credential_fetch_error(fetch_field, data, baseline, serial) from err
        _LOGGER.warning("RTSP auth failed for camera %s", data.get(ATTR_SERIAL))
        auth_error = AuthTestResultFailed("RTSP DESCRIBE auth test failed")
        cast(Any, auth_error).data = data
        raise auth_error from err

    except PyEzvizError as err:
        if fetch_field is not None:
            raise _credential_fetch_error(fetch_field, data, baseline, serial) from err
        _LOGGER.warning(
            "EZVIZ API error while preparing/testing %s", data.get(ATTR_SERIAL)
        )
        api_error = PyEzvizError("EZVIZ API error during RTSP credential testing")
        cast(Any, api_error).data = data
        raise api_error from err

    # Remove ephemeral values before returning
    data.pop(CONF_CAM_VERIFICATION_2FA_CODE, None)
    data.pop(CONF_CAM_ENC_2FA_CODE, None)
    data.pop("ephemeral_test_rtsp", None)
    data.pop(CONF_IP_ADDRESS, None)
    data.pop("cloud_account_username")

    return data
