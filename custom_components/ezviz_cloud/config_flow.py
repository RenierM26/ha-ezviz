"""Canonical HA account setup/reauth flow and options factory for EZVIZ Cloud."""

import asyncio
from collections.abc import Mapping
import logging
from typing import Any, override

import probatio
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)

from homeassistant import config_entries
from homeassistant.config_entries import HANDLERS, ConfigEntry, ConfigFlowResult
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .const import (
    ATTR_TYPE_CLOUD,
    CONF_AUTH_REF,
    CONF_REGION,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
    REGION_CUSTOM,
    REGION_EU,
    REGION_RU,
    REGION_URLS,
)
from .flow_validation import resolve_api_host, validate_timeout
from .options_flow import EzvizOptionsFlowHandler
from .runtime import EzvizConfigEntry
from .tokens import (
    TokenPersistence,
    async_login_identity,
    credential_login,
    stage_login,
)
from .workers import create_critical_task, create_executor_task

_LOGGER = logging.getLogger(__name__)

VERSION = 5  # keep in sync with __init__.py TARGET_VERSION


class EzvizConfigFlow(config_entries.ConfigFlow):
    """Handle the cloud account config flow for EZVIZ."""

    VERSION = VERSION

    _reauth_entry: ConfigEntry[Any]
    _reauth_username: str
    _reauth_password: str
    _reauth_url: str
    _reauth_timeout: int

    _pending_user_username: str
    _pending_user_password: str
    _pending_user_url: str
    _pending_user_timeout: int = DEFAULT_TIMEOUT

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: EzvizConfigEntry,
    ) -> EzvizOptionsFlowHandler:
        """Get the options flow handler."""
        return EzvizOptionsFlowHandler(config_entry)

    # --------------------------
    # Initial user setup (with MFA)
    # --------------------------

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a single cloud account entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                api_url = resolve_api_host(
                    user_input[CONF_REGION], user_input.get(CONF_URL)
                )
            except probatio.Invalid:
                errors["base"] = "invalid_url"

            try:
                timeout = validate_timeout(
                    user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                )
            except probatio.Invalid:
                errors[CONF_TIMEOUT] = "invalid_timeout"

            if not errors:
                try:
                    client = EzvizClient(
                        account=username,
                        password=password,
                        url=api_url,
                        timeout=timeout,
                        feature_code=await async_login_identity(self.hass, username),
                    )
                    # First attempt without SMS -> returns a token dict on success
                    token = await asyncio.shield(
                        create_executor_task(self.hass, credential_login, client)
                    )

                except OSError:
                    return await self._async_storage_error("user")

                except EzvizAuthVerificationCode:
                    # Stash pending values; request SMS code
                    self._pending_user_username = username
                    self._pending_user_password = password
                    self._pending_user_url = api_url
                    self._pending_user_timeout = timeout
                    return await self.async_step_user_mfa_confirm()

                except InvalidURL, HTTPError, PyEzvizError:
                    errors["base"] = "cannot_connect"

                except Exception:
                    _LOGGER.exception("Unexpected error during EZVIZ login")
                    errors["base"] = "unknown"

                else:
                    return await self._async_create_cloud_entry(
                        token, username, api_url, timeout, "user"
                    )

        # Show form
        schema = probatio.Schema(
            {
                probatio.Required(CONF_USERNAME): str,
                probatio.Required(CONF_PASSWORD): str,
                probatio.Required(CONF_REGION, default=REGION_EU): probatio.In(
                    [REGION_EU, REGION_RU, REGION_CUSTOM]
                ),
                probatio.Optional(CONF_URL): str,  # required only when region == custom
                # Custom callable validators cannot be serialized into an HA form.
                probatio.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): probatio.All(
                    int, probatio.Range(min=1, max=300)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_user_mfa_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA during initial cloud setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sms_code = user_input.get("sms_code", "")

            try:
                client = EzvizClient(
                    account=self._pending_user_username,
                    password=self._pending_user_password,
                    url=self._pending_user_url,
                    timeout=self._pending_user_timeout,
                    feature_code=await async_login_identity(
                        self.hass, self._pending_user_username
                    ),
                )
                token = await asyncio.shield(
                    create_executor_task(self.hass, credential_login, client, sms_code)
                )

            except OSError:
                return await self._async_storage_error("user_mfa_confirm")

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"
            except InvalidURL, HTTPError, PyEzvizError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during initial MFA")
                errors["base"] = "unknown"
            else:
                return await self._async_create_cloud_entry(
                    token,
                    self._pending_user_username,
                    self._pending_user_url,
                    self._pending_user_timeout,
                    "user_mfa_confirm",
                )

        schema = probatio.Schema({probatio.Required("sms_code"): str})
        return self.async_show_form(
            step_id="user_mfa_confirm", data_schema=schema, errors=errors
        )

    # --------------------------
    # Reauth (cloud) + MFA
    # --------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for the EZVIZ account."""
        entry = self._get_reauth_entry()
        if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
            return self.async_abort(reason="unknown")

        await self.async_set_unique_id(entry.unique_id)
        self._reauth_entry = entry
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect password and try login; may require 2FA."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._reauth_username = user_input[CONF_USERNAME]
            self._reauth_password = user_input[CONF_PASSWORD]
            self._reauth_url = self._reauth_entry.data.get(
                CONF_URL, REGION_URLS[REGION_EU]
            )
            try:
                self._reauth_timeout = validate_timeout(
                    self._reauth_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                )
            except probatio.Invalid:
                self._reauth_timeout = DEFAULT_TIMEOUT

            try:
                client = EzvizClient(
                    account=self._reauth_username,
                    password=self._reauth_password,
                    url=self._reauth_url,
                    timeout=self._reauth_timeout,
                    feature_code=await TokenPersistence(
                        self.hass, self._reauth_entry
                    ).async_identity(),
                )
                token = await asyncio.shield(
                    create_executor_task(self.hass, credential_login, client)
                )

            except OSError:
                return await self._async_storage_error("reauth_confirm")

            except EzvizAuthVerificationCode:
                return await self.async_step_reauth_mfa()

            except InvalidURL, HTTPError, PyEzvizError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                # Refresh tokens and restore the internal user id missing from legacy entries.
                return await self._async_finish_reauth(token, "reauth_confirm")

        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_USERNAME,
                    default=self._reauth_entry.unique_id
                    or self._reauth_entry.data.get(CONF_USERNAME, ""),
                ): (
                    probatio.In(
                        [
                            self._reauth_entry.unique_id
                            or self._reauth_entry.data[CONF_USERNAME]
                        ]
                    )
                    if self._reauth_entry.unique_id
                    or self._reauth_entry.data.get(CONF_USERNAME)
                    else str
                ),
                probatio.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    async def async_step_reauth_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect SMS code and complete reauth."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                client = EzvizClient(
                    account=self._reauth_username,
                    password=self._reauth_password,
                    url=self._reauth_url,
                    timeout=self._reauth_timeout,
                    feature_code=await TokenPersistence(
                        self.hass, self._reauth_entry
                    ).async_identity(),
                )
                token = await asyncio.shield(
                    create_executor_task(
                        self.hass, credential_login, client, user_input["sms_code"]
                    )
                )

            except OSError:
                return await self._async_storage_error("reauth_mfa")

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"
            except InvalidURL, HTTPError, PyEzvizError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth MFA")
                errors["base"] = "unknown"
            else:
                return await self._async_finish_reauth(token, "reauth_mfa")

        schema = probatio.Schema({probatio.Required("sms_code"): str})
        return self.async_show_form(
            step_id="reauth_mfa", data_schema=schema, errors=errors
        )

    async def _async_storage_error(self, step: str) -> ConfigFlowResult:
        result = await getattr(self, f"async_step_{step}")()
        result["errors"] = {"base": "cannot_save_credentials"}
        return result

    async def _async_create_cloud_entry(
        self, token: dict, username: str, api_url: str, timeout: int, step: str
    ) -> ConfigFlowResult:
        try:
            reference = await stage_login(self.hass, username, token)
        except OSError:
            return await self._async_storage_error(step)
        return self.async_create_entry(
            title=username,
            data={
                CONF_TYPE: ATTR_TYPE_CLOUD,
                CONF_URL: api_url,
                CONF_AUTH_REF: reference,
            },
            options={CONF_TIMEOUT: timeout, OPTIONS_KEY_CAMERAS: {}},
        )

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Promote the staged login after HA allocates the entry."""
        entry = result["result"]
        assert entry is not None
        # async_add performs setup before this hook. Ensure promotion also happened
        # when setup was skipped/disabled, without exposing a token in entry data.
        try:
            await TokenPersistence(self.hass, entry).async_load()
        except OSError:
            # Keep only the private staging reference so HA setup can retry.
            _LOGGER.warning("EZVIZ credential promotion is pending; setup must retry")
        result["data"] = dict(entry.data)
        return result

    async def _async_finish_reauth(self, token: dict, step: str) -> ConfigFlowResult:
        async def finish() -> ConfigFlowResult:
            try:
                await TokenPersistence(self.hass, self._reauth_entry).async_replace(
                    token, api_url=self._reauth_url, login_name=self._reauth_username
                )
            except OSError:
                return await self._async_storage_error(step)
            return self.async_update_reload_and_abort(
                self._reauth_entry, reload_even_if_entry_is_unchanged=True
            )

        task = create_critical_task(
            self.hass, finish(), "EZVIZ reauthentication commit"
        )
        return await asyncio.shield(task)


# Register the canonical account flow for this custom domain.
HANDLERS.register(DOMAIN)(EzvizConfigFlow)
