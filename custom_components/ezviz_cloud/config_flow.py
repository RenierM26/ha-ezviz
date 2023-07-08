"""Config flow for EZVIZ."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyezviz.client import EzvizClient
from pyezviz.exceptions import (
    AuthTestResultFailed,
    EzvizAuthVerificationCode,
    InvalidHost,
    InvalidURL,
    PyEzvizError,
)
from pyezviz.test_cam_rtsp import TestRTSPAuth
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
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
from homeassistant.data_entry_flow import FlowResult

from .const import (
    ATTR_SERIAL,
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RF_SESSION_ID,
    CONF_SESSION_ID,
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

    test_rtsp = TestRTSPAuth(
        data[CONF_IP_ADDRESS], data[CONF_USERNAME], data[CONF_PASSWORD]
    )

    test_rtsp.main()


def _wake_camera(data, ezviz_token, ezviz_timeout):
    """Wake up hibernating camera and test."""
    ezviz_client = EzvizClient(token=ezviz_token, timeout=ezviz_timeout)

    # We need to wake hibernating cameras.
    # First create EZVIZ API instance.
    ezviz_client.login()

    # Secondly try to wake hybernating camera.
    ezviz_client.get_detection_sensibility(data[ATTR_SERIAL])

    # Thirdly attempts an authenticated RTSP DESCRIBE request.
    _test_camera_rtsp_creds(data)


class EzvizConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EZVIZ."""

    VERSION = 1
    ezviz_client: EzvizClient
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

        auth_data = {
            CONF_USERNAME: data[CONF_USERNAME],
            CONF_SESSION_ID: ezviz_token[CONF_SESSION_ID],
            CONF_RF_SESSION_ID: ezviz_token[CONF_RF_SESSION_ID],
            CONF_URL: ezviz_token["api_url"],
            CONF_TYPE: ATTR_TYPE_CLOUD,
        }

        return auth_data

    async def _validate_and_create_camera_rtsp(self, data: dict) -> FlowResult:
        """Try DESCRIBE on RTSP camera with credentials."""

        # Get EZVIZ cloud credentials from config entry
        ezviz_token = {
            CONF_SESSION_ID: None,
            CONF_RF_SESSION_ID: None,
            "api_url": None,
        }
        ezviz_timeout = DEFAULT_TIMEOUT

        for item in self._async_current_entries():
            if item.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD:
                ezviz_token = {
                    CONF_SESSION_ID: item.data.get(CONF_SESSION_ID),
                    CONF_RF_SESSION_ID: item.data.get(CONF_RF_SESSION_ID),
                    "api_url": item.data.get(CONF_URL),
                }
                ezviz_timeout = item.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

        # Abort flow if user removed cloud account before adding camera.
        if ezviz_token.get(CONF_SESSION_ID) is None:
            return self.async_abort(reason="ezviz_cloud_account_missing")

        await self.hass.async_add_executor_job(
            _wake_camera, data, ezviz_token, ezviz_timeout
        )

        return self.async_create_entry(
            title=data[ATTR_SERIAL],
            data={
                CONF_USERNAME: data[CONF_USERNAME],
                CONF_PASSWORD: data[CONF_PASSWORD],
                CONF_TYPE: ATTR_TYPE_CAMERA,
            },
            options=DEFAULT_OPTIONS,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EzvizOptionsFlowHandler:
        """Get the options flow for this handler."""
        return EzvizOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                self.context["data"] = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
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
                self.context["data"] = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_URL: user_input[CONF_URL],
                }
                return await self.async_step_user_mfa_confirm()

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:  # pylint: disable=broad-except
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
    ) -> FlowResult:
        """Handle a flow initiated by the user for custom region url."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.context["data"][CONF_USERNAME]
            user_input[CONF_PASSWORD] = self.context["data"][CONF_PASSWORD]

            try:
                auth_data = await self.hass.async_add_executor_job(
                    self._validate_and_create_auth, user_input
                )

            except InvalidURL:
                errors["base"] = "invalid_host"

            except InvalidHost:
                errors["base"] = "cannot_connect"

            except EzvizAuthVerificationCode:
                self.context["data"] = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_URL: user_input[CONF_URL],
                }
                return await self.async_step_user_mfa_confirm()

            except PyEzvizError:
                errors["base"] = "invalid_auth"

            except Exception:  # pylint: disable=broad-except
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
    ) -> FlowResult:
        """Handle a flow for discovered camera without rtsp config entry."""

        await self.async_set_unique_id(discovery_info[ATTR_SERIAL])
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {ATTR_SERIAL: self.unique_id}
        self.context["data"] = {CONF_IP_ADDRESS: discovery_info[CONF_IP_ADDRESS]}

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm and create entry from discovery step."""
        errors = {}

        if user_input is not None:
            user_input[ATTR_SERIAL] = self.unique_id
            user_input[CONF_IP_ADDRESS] = self.context["data"][CONF_IP_ADDRESS]
            try:
                return await self._validate_and_create_camera_rtsp(user_input)

            except (InvalidHost, InvalidURL):
                errors["base"] = "invalid_host"

            except EzvizAuthVerificationCode:
                errors["base"] = "mfa_required"

            except (PyEzvizError, AuthTestResultFailed):
                errors["base"] = "invalid_auth"

            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

        discovered_camera_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_CAMERA_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="confirm",
            data_schema=discovered_camera_schema,
            errors=errors,
            description_placeholders={
                ATTR_SERIAL: self.unique_id,
                CONF_IP_ADDRESS: self.context["data"][CONF_IP_ADDRESS],
            },
        )

    async def async_step_reauth(self, user_input: Mapping[str, Any]) -> FlowResult:
        """Handle a flow for reauthentication with password."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                self.context["data"] = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_URL: user_input[CONF_URL],
                }
                return await self.async_step_reauth_mfa()

            except (PyEzvizError, AuthTestResultFailed):
                errors["base"] = "invalid_auth"

            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=auth_data,
                )

                await self.hass.config_entries.async_reload(entry.entry_id)

                return self.async_abort(reason="reauth_successful")

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
    ) -> FlowResult:
        """Handle a MFA based authentication flow for reauth."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.context["data"][CONF_USERNAME]
            user_input[CONF_PASSWORD] = self.context["data"][CONF_PASSWORD]
            user_input[CONF_URL] = self.context["data"][CONF_URL]

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

            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")

            else:
                self.hass.config_entries.async_update_entry(
                    self.entry_data,
                    data=auth_data,
                )

                await self.hass.config_entries.async_reload(self.entry_data.entry_id)

                return self.async_abort(reason="reauth_successful")

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
    ) -> FlowResult:
        """Handle a MFA based user initiated authentication flow."""
        errors = {}
        auth_data = {}

        if user_input is not None:
            user_input[CONF_USERNAME] = self.context["data"][CONF_USERNAME]
            user_input[CONF_PASSWORD] = self.context["data"][CONF_PASSWORD]
            user_input[CONF_URL] = self.context["data"][CONF_URL]

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

            except Exception:  # pylint: disable=broad-except
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


class EzvizOptionsFlowHandler(OptionsFlow):
    """Handle EZVIZ client options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
