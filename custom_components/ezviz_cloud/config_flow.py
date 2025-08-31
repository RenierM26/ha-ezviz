"""Config flow for EZVIZ."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    InvalidHost,
    InvalidURL,
    PyEzvizError,
)
from pyezvizapi.test_cam_rtsp import TestRTSPAuth
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_CUSTOMIZE,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .const import (
    ATTR_SERIAL,
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_CAM_ENC_2FA_CODE,
    CONF_CAM_VERIFICATION_2FA_CODE,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RF_SESSION_ID,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_SESSION_ID,
    CONF_TEST_RTSP_CREDENTIALS,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    EU_URL,
    RUSSIA_URL,
)

_LOGGER = logging.getLogger(__name__)
DEFAULT_OPTIONS = {
    CONF_FFMPEG_ARGUMENTS: DEFAULT_FFMPEG_ARGUMENTS,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
}


def _test_camera_rtsp_creds(data: dict) -> None:
    """Try DESCRIBE on RTSP camera with credentials."""

    # First test with verification code, then try encryption key.
    # Newer cameras use encryption key if set, older cameras use verification code.
    if data[CONF_RTSP_USES_VERIFICATION_CODE]:
        test_rtsp = TestRTSPAuth(
            data[CONF_IP_ADDRESS], data[CONF_USERNAME], data[CONF_PASSWORD]
        )
        test_rtsp.main()

    else:
        test_rtsp = TestRTSPAuth(
            data[CONF_IP_ADDRESS], data[CONF_USERNAME], data[CONF_ENC_KEY]
        )
        test_rtsp.main()


def _wake_camera(data: dict, ezviz_client: EzvizClient) -> None:
    """Wake up hibernating camera and test."""

    # Wake hybernating camera.
    ezviz_client.get_detection_sensibility(data[ATTR_SERIAL])

    # Attempts an authenticated RTSP DESCRIBE request.
    _test_camera_rtsp_creds(data)


def _get_cam_verification_code(data: dict, ezviz_client: EzvizClient) -> Any:
    """Get camera verification code."""
    _LOGGER.warning("Getting camera verification code for %s", data[ATTR_SERIAL])
    try:
        return ezviz_client.get_cam_auth_code(
            data[ATTR_SERIAL],
            msg_auth_code=data.get(CONF_CAM_VERIFICATION_2FA_CODE),
            sender_type=0 if data.get(CONF_CAM_VERIFICATION_2FA_CODE) else 3,
        )

    except EzvizAuthVerificationCode as err:
        ezviz_client.get_2fa_check_code(username=data["cloud_account_username"], biz_type="DEVICE_AUTH_CODE")
        raise EzvizAuthVerificationCode from err


def _get_cam_enc_key(data: dict, ezviz_client: EzvizClient) -> Any:
    """Get camera encryption key."""
    _LOGGER.warning("Getting camera encryption key for %s", data[ATTR_SERIAL])
    try:
        return ezviz_client.get_cam_key(
            data[ATTR_SERIAL], smscode=data.get(CONF_CAM_ENC_2FA_CODE)
        )

    except EzvizAuthVerificationCode as err:
        # Triggers sending of 2FA code, no need to request.
        raise EzvizAuthVerificationCode from err


class EzvizConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EZVIZ."""

    VERSION = 3

    ip_address: str
    username: str | None
    password: str | None
    enc_key: str | None
    rtsp_uses_verification_code: bool | None
    test_rtsp_credentials: bool | None
    ezviz_url: str | None
    unique_id: str
    ezviz_client: EzvizClient = None
    entry_data: ConfigEntry

    def _validate_and_create_auth(self, data: dict) -> dict[str, Any]:
        """Try to login to EZVIZ cloud account and return token."""
        # Verify cloud credentials by attempting a login request with username and password.
        # Return login token.

        self.ezviz_client = EzvizClient(
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data[CONF_URL],
            data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        )

        ezviz_token = self.ezviz_client.login(sms_code=data.get("sms_code"))

        return {
            CONF_USERNAME: data[CONF_USERNAME],
            CONF_SESSION_ID: ezviz_token[CONF_SESSION_ID],
            CONF_RF_SESSION_ID: ezviz_token[CONF_RF_SESSION_ID],
            CONF_URL: ezviz_token["api_url"],
            CONF_USER_ID: ezviz_token["username"],
            CONF_TYPE: ATTR_TYPE_CLOUD,
        }

    async def _validate_and_create_camera_rtsp(self, data: dict) -> ConfigFlowResult:
        """Try DESCRIBE on RTSP camera with credentials."""

        for item in self.hass.config_entries.async_entries(
            domain=DOMAIN, include_ignore=False
        ):
            if item.data[CONF_TYPE] == ATTR_TYPE_CLOUD:
                data["cloud_account_username"] = item.data[CONF_USERNAME]
                self.ezviz_client = self.hass.data[DOMAIN][item.entry_id][
                    DATA_COORDINATOR
                ].ezviz_client

        # Abort flow if user removed cloud account before adding camera.
        if self.ezviz_client is None:
            return self.async_abort(reason="ezviz_cloud_account_missing")

        # Fetch encryption key. 2FA code is required for this to work.
        if data[CONF_ENC_KEY] == "fetch_my_key":
            data[CONF_ENC_KEY] = await self.hass.async_add_executor_job(
                _get_cam_enc_key, data, self.ezviz_client
            )
            _LOGGER.warning("Fetched camera encryption key for %s", data[ATTR_SERIAL])

        # Fetch camera sticker code from ezviz api. 2FA code is required for this to work.
        if data[CONF_PASSWORD] == "fetch_my_key":
            data[CONF_PASSWORD] = await self.hass.async_add_executor_job(
                _get_cam_verification_code, data, self.ezviz_client
            )
            _LOGGER.warning(
                "Fetched camera verification code for %s", data[ATTR_SERIAL]
            )

        if data[CONF_TEST_RTSP_CREDENTIALS]:
            await self.hass.async_add_executor_job(
                _wake_camera, data, self.ezviz_client
            )

        return self.async_create_entry(
            title=data[ATTR_SERIAL],
            data={
                CONF_USERNAME: data[CONF_USERNAME],
                CONF_PASSWORD: data[CONF_PASSWORD],
                CONF_ENC_KEY: data[CONF_ENC_KEY],
                CONF_RTSP_USES_VERIFICATION_CODE: data[
                    CONF_RTSP_USES_VERIFICATION_CODE
                ],
                CONF_TYPE: ATTR_TYPE_CAMERA,
            },
            options=DEFAULT_OPTIONS,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EzvizOptionsFlowHandler:
        """Get the options flow for this handler."""
        return EzvizOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""

        # Check if EZVIZ cloud account is present in entry config,
        # abort if already configured.
        for item in self._async_current_entries():
            if item.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD:
                return self.async_abort(reason="already_configured_account")

        errors = {}
        auth_data = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            if user_input[CONF_URL] == CONF_CUSTOMIZE:
                self.username = user_input[CONF_USERNAME]
                self.password = user_input[CONF_PASSWORD]

                return await self.async_step_user_custom_url()

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except InvalidURL:
                errors["base"] = "invalid_host"

            except InvalidHost:
                errors["base"] = "cannot_connect"

            except EzvizAuthVerificationCode:
                self.username = user_input[CONF_USERNAME]
                self.password = user_input[CONF_PASSWORD]
                self.ezviz_url = user_input[CONF_URL]

                return await self.async_step_user_mfa_confirm()

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=auth_data,
                    options=DEFAULT_OPTIONS,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_URL, default=EU_URL): vol.In(
                    [EU_URL, RUSSIA_URL, CONF_CUSTOMIZE]
                ),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_user_custom_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user for custom region url."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.username
            user_input[CONF_PASSWORD] = self.password

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except InvalidURL:
                errors["base"] = "invalid_host"

            except InvalidHost:
                errors["base"] = "cannot_connect"

            except EzvizAuthVerificationCode:
                self.ezviz_url = user_input[CONF_URL]

                return await self.async_step_user_mfa_confirm()

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=auth_data,
                    options=DEFAULT_OPTIONS,
                )

        data_schema_custom_url = vol.Schema(
            {
                vol.Required(CONF_URL, default=EU_URL): str,
            }
        )

        return self.async_show_form(
            step_id="user_custom_url", data_schema=data_schema_custom_url, errors=errors
        )

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a flow for discovered camera without rtsp config entry."""

        await self.async_set_unique_id(discovery_info[ATTR_SERIAL])
        self._abort_if_unique_id_configured()

        if TYPE_CHECKING:
            # A unique ID is passed in via the discovery info
            assert self.unique_id is not None

        self.context["title_placeholders"] = {ATTR_SERIAL: self.unique_id}
        self.ip_address = discovery_info[CONF_IP_ADDRESS]

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create entry from discovery step."""
        errors = {}

        if user_input is not None:
            user_input[ATTR_SERIAL] = self.unique_id
            user_input[CONF_IP_ADDRESS] = self.ip_address
            try:
                return await self._validate_and_create_camera_rtsp(user_input)

            except (InvalidHost, InvalidURL):
                errors["base"] = "invalid_host"

            except EzvizAuthVerificationCode:
                self.username = user_input[CONF_USERNAME]
                self.password = user_input[CONF_PASSWORD]
                self.enc_key = user_input[CONF_ENC_KEY]
                self.rtsp_uses_verification_code = user_input[
                    CONF_RTSP_USES_VERIFICATION_CODE
                ]
                self.test_rtsp_credentials = user_input[CONF_TEST_RTSP_CREDENTIALS]

                return await self.async_step_confirm_2FA()

            except DeviceException:
                errors["base"] = "device_exception"

            except (PyEzvizError, AuthTestResultFailed):
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

        discovered_camera_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_CAMERA_USERNAME): str,
                vol.Required(CONF_PASSWORD, default="fetch_my_key"): str,
                vol.Required(CONF_ENC_KEY, default="fetch_my_key"): str,
                vol.Optional(CONF_RTSP_USES_VERIFICATION_CODE, default=False): bool,
                vol.Optional(CONF_TEST_RTSP_CREDENTIALS, default=True): bool,
            }
        )

        return self.async_show_form(
            step_id="confirm",
            data_schema=discovered_camera_schema,
            errors=errors,
            description_placeholders={
                ATTR_SERIAL: self.unique_id,
                CONF_IP_ADDRESS: self.ip_address,
            },
        )

    async def async_step_confirm_2FA(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create entry from discovery step when 2FA is needed."""
        errors = {}

        if user_input is not None:
            user_input[ATTR_SERIAL] = self.unique_id
            user_input[CONF_IP_ADDRESS] = self.ip_address
            user_input[CONF_USERNAME] = self.username
            user_input[CONF_PASSWORD] = self.password
            user_input[CONF_ENC_KEY] = self.enc_key
            user_input[CONF_RTSP_USES_VERIFICATION_CODE] = (
                self.rtsp_uses_verification_code
            )
            user_input[CONF_TEST_RTSP_CREDENTIALS] = self.test_rtsp_credentials

            try:
                return await self._validate_and_create_camera_rtsp(user_input)

            except (InvalidHost, InvalidURL):
                errors["base"] = "invalid_host"

            except DeviceException:
                errors["base"] = "device_exception"

            except (PyEzvizError, AuthTestResultFailed):
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

        discovered_camera_schema = vol.Schema(
            {
                vol.Optional(CONF_CAM_VERIFICATION_2FA_CODE, default="1234"): str,
                vol.Optional(CONF_CAM_ENC_2FA_CODE, default="1234"): str,
            }
        )

        return self.async_show_form(
            step_id="confirm_2FA",
            data_schema=discovered_camera_schema,
            errors=errors,
            description_placeholders={
                ATTR_SERIAL: self.unique_id,
                CONF_IP_ADDRESS: self.ip_address,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a flow for reauthentication with password."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a Confirm flow for reauthentication with password."""
        auth_data = {}
        errors = {}
        entry = None

        for item in self._async_current_entries():
            if item.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD:
                self.context["title_placeholders"] = {ATTR_SERIAL: item.title}
                entry = await self.async_set_unique_id(item.unique_id)

        if not entry:
            return self.async_abort(reason="ezviz_cloud_account_missing")

        if user_input is not None:
            user_input[CONF_URL] = entry.data[CONF_URL]

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except (InvalidHost, InvalidURL):
                errors["base"] = "invalid_host"

            except EzvizAuthVerificationCode:
                self.entry_data = entry
                self.username = user_input[CONF_USERNAME]
                self.password = user_input[CONF_PASSWORD]
                self.ezviz_url = user_input[CONF_URL]

                return await self.async_step_reauth_mfa()

            except (PyEzvizError, AuthTestResultFailed):
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data=auth_data,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.unique_id): vol.In(
                    [entry.unique_id]
                ),
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_reauth_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a MFA based authentication flow for reauth."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.username
            user_input[CONF_PASSWORD] = self.password
            user_input[CONF_URL] = self.ezviz_url

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except InvalidURL:
                errors["base"] = "invalid_host"

            except InvalidHost:
                errors["base"] = "cannot_connect"

            except EzvizAuthVerificationCode:
                errors["base"] = "mfa_required"

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                return self.async_update_reload_and_abort(
                    self.entry_data,
                    data=auth_data,
                )

        data_schema_mfa_code = vol.Schema(
            {
                vol.Required("sms_code"): str,
            }
        )

        return self.async_show_form(
            step_id="reauth_mfa",
            data_schema=data_schema_mfa_code,
            errors=errors,
        )

    async def async_step_user_mfa_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a MFA based user initiated authentication flow."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.username
            user_input[CONF_PASSWORD] = self.password
            user_input[CONF_URL] = self.ezviz_url

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except InvalidURL:
                errors["base"] = "invalid_host"

            except InvalidHost:
                errors["base"] = "cannot_connect"

            except EzvizAuthVerificationCode:
                errors["base"] = "mfa_required"

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=auth_data,
                    options=DEFAULT_OPTIONS,
                )

        data_schema_mfa_code = vol.Schema(
            {
                vol.Required("sms_code"): str,
            }
        )

        return self.async_show_form(
            step_id="user_mfa_confirm", data_schema=data_schema_mfa_code, errors=errors
        )


class EzvizOptionsFlowHandler(OptionsFlowWithReload):
    """Handle EZVIZ client options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage EZVIZ options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = vol.Schema(
            {
                vol.Optional(
                    CONF_TIMEOUT,
                    default=self.config_entry.options.get(
                        CONF_TIMEOUT, DEFAULT_TIMEOUT
                    ),
                ): int,
                vol.Optional(
                    CONF_FFMPEG_ARGUMENTS,
                    default=self.config_entry.options.get(
                        CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                    ),
                ): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=options)
