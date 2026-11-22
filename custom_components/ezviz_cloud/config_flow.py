"""Canonical HA account setup/reauth flow and options factory for EZVIZ Cloud."""

import asyncio
from collections.abc import Mapping
import logging
from typing import Any, override

import probatio
from pyezvizapi.client import EzvizClient
from pyezvizapi.cloud.challenges import CHANNEL_NAMES, PHONE_CHANNELS
from pyezvizapi.exceptions import (
    EzvizAuthVerificationCode,
    EzvizLoginError,
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
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    ATTR_TYPE_CLOUD,
    CONF_AUTH_REF,
    CONF_REGION,
    CONFIG_ENTRY_MINOR_VERSION,
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

_APP_VERIFICATION_REASONS = {
    "captcha_required",
    "captcha_incorrect",
    "security_verification_required",
    "verification_contact_required",
    "verification_phone_unavailable",
}
_LOGIN_ERRORS = {
    "incorrect_username": "invalid_auth",
    "incorrect_password": "invalid_auth",
    "incorrect_credentials": "invalid_auth",
    "verification_incorrect": "verification_incorrect",
    "verification_expired": "verification_expired",
    "verification_rate_limited": "verification_rate_limited",
    "verification_attempts_exceeded": "verification_rate_limited",
    "account_locked": "account_locked",
    "terminal_limit": "terminal_limit",
    "region_selection_required": "region_selection_required",
    "clock_incorrect": "clock_incorrect",
}


def _login_error(error: EzvizLoginError) -> str:
    if error.reason in _APP_VERIFICATION_REASONS:
        return "verification_in_app"
    return _LOGIN_ERRORS.get(error.reason, "cannot_connect")


VERSION = 5  # keep in sync with __init__.py TARGET_VERSION


class EzvizConfigFlow(config_entries.ConfigFlow):
    """Handle the cloud account config flow for EZVIZ."""

    VERSION = VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    _reauth_entry: ConfigEntry[Any]
    _reauth_username: str
    _reauth_url: str
    _reauth_timeout: int

    _pending_user_username: str
    _pending_user_url: str
    _pending_user_timeout: int = DEFAULT_TIMEOUT

    _verification_identity: str
    _delivery_task: asyncio.Task[ConfigFlowResult] | None = None
    _verification_client: EzvizClient
    _verification_step: str
    _verification_channels: tuple[int, ...] = ()
    _verification_channel: int | None = None

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
                    identity = await async_login_identity(self.hass, username)
                    client = EzvizClient(
                        account=username,
                        password=password,
                        url=api_url,
                        timeout=timeout,
                        feature_code=identity,
                    )
                    # First attempt without SMS -> returns a token dict on success
                    token = await asyncio.shield(
                        create_executor_task(self.hass, credential_login, client)
                    )

                except OSError:
                    return await self._async_storage_error("user")

                except EzvizAuthVerificationCode as error:
                    self._pending_user_username = username
                    self._pending_user_url = api_url
                    self._pending_user_timeout = timeout
                    return self._start_verification(
                        client, identity, "user_mfa_confirm", error
                    )

                except EzvizLoginError as error:
                    errors["base"] = _login_error(error)

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
        """Complete verification during initial setup."""
        return await self._async_verify("user_mfa_confirm", user_input)

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
                identity = await TokenPersistence(
                    self.hass, self._reauth_entry
                ).async_identity()
                client = EzvizClient(
                    account=self._reauth_username,
                    password=user_input[CONF_PASSWORD],
                    url=self._reauth_url,
                    timeout=self._reauth_timeout,
                    feature_code=identity,
                )
                token = await asyncio.shield(
                    create_executor_task(self.hass, credential_login, client)
                )

            except OSError:
                return await self._async_storage_error("reauth_confirm")

            except EzvizAuthVerificationCode as error:
                return self._start_verification(client, identity, "reauth_mfa", error)

            except EzvizLoginError as error:
                errors["base"] = _login_error(error)

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
        """Complete verification during reauthentication."""
        return await self._async_verify("reauth_mfa", user_input)

    def _start_verification(
        self,
        client: EzvizClient,
        identity: str,
        step: str,
        error: EzvizAuthVerificationCode,
    ) -> ConfigFlowResult:
        # Keep the same SDK client: it owns bound contacts, regional policy and
        # the selected method. credential_login already closed its transport.
        self._verification_client = client
        self._verification_identity = identity
        self._verification_step = step
        self._verification_channel = None
        self._update_delivery_options(error)
        if self._verification_channels and not error.code_sent:
            return self._show_delivery()
        return self._show_verification(
            step,
            "verification_in_app"
            if error.reason in _APP_VERIFICATION_REASONS
            else None,
        )

    def _update_delivery_options(self, error: EzvizAuthVerificationCode) -> None:
        self._verification_channels = tuple(
            channel for channel in error.available_channels if channel in CHANNEL_NAMES
        )
        if (
            not self._verification_channels
            and error.code == 6002
            and error.reason == "verification_required"
        ):
            # The SDK supports legacy email challenges without contact metadata
            # and validates the destination before sending.
            self._verification_channels = (3,)
        if self._verification_channel not in self._verification_channels:
            self._verification_channel = None

    def _show_verification(
        self, step: str, error: str | None = None
    ) -> ConfigFlowResult:
        schema: dict[Any, Any] = {probatio.Optional("sms_code"): str}
        if self._verification_channels:
            schema[probatio.Optional("change_delivery")] = bool
        return self.async_show_form(
            step_id=step,
            data_schema=probatio.Schema(schema),
            errors={"base": error} if error else {},
        )

    async def _async_verify(
        self, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self._show_verification(step)
        if user_input.get("change_delivery") and self._verification_channels:
            return self._show_delivery()
        code = user_input.get("sms_code", "").strip()
        if not code or not code.isascii() or not code.isdigit():
            return self._show_verification(step, "verification_required")

        try:
            if step == "reauth_mfa":
                identity = await TokenPersistence(
                    self.hass, self._reauth_entry
                ).async_identity()
            else:
                identity = await async_login_identity(
                    self.hass, self._pending_user_username
                )
            token = None
            if identity == self._verification_identity:
                token = await asyncio.shield(
                    create_executor_task(
                        self.hass,
                        credential_login,
                        self._verification_client,
                        code,
                        self._verification_channel,
                    )
                )
        except OSError:
            return await self._async_storage_error(step)
        except EzvizAuthVerificationCode as error:
            previous_channels = self._verification_channels
            previous_channel = self._verification_channel
            self._update_delivery_options(error)
            message = (
                "verification_required"
                if error.reason == "verification_required"
                else _login_error(error)
            )
            if self._verification_channels and (
                self._verification_channel is None
                and (
                    previous_channel is not None
                    or previous_channels != self._verification_channels
                )
            ):
                return self._show_delivery(message)
            return self._show_verification(step, message)
        except EzvizLoginError as error:
            return self._show_verification(step, _login_error(error))
        except InvalidURL, HTTPError, PyEzvizError:
            return self._show_verification(step, "cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected error during EZVIZ verification")
            return self._show_verification(step, "unknown")

        if token is None:
            result = (
                await self.async_step_reauth_confirm()
                if step == "reauth_mfa"
                else await self.async_step_user()
            )
            result["errors"] = {"base": "verification_identity_changed"}
            return result
        if step == "reauth_mfa":
            return await self._async_finish_reauth(token, step)
        return await self._async_create_cloud_entry(
            token,
            self._pending_user_username,
            self._pending_user_url,
            self._pending_user_timeout,
            step,
        )

    def _show_delivery(self, error: str | None = None) -> ConfigFlowResult:
        channels = self._verification_channels
        selected = self._verification_channel
        options = [CHANNEL_NAMES[channel] for channel in channels]
        schema: dict[Any, Any] = {
            probatio.Required(
                "method",
                default=CHANNEL_NAMES[
                    selected if selected in channels else channels[0]
                ],
            ): SelectSelector(
                SelectSelectorConfig(options=options, translation_key="mfa_method")
            ),
            probatio.Optional("use_existing_code"): bool,
        }
        if 1 in channels:
            schema[probatio.Optional("country_code")] = str
        return self.async_show_form(
            step_id="mfa_delivery",
            data_schema=probatio.Schema(schema),
            errors={"base": error} if error else {},
        )

    def _manage_delivery(self, channel: int, country_code: str) -> tuple[int, ...]:
        client = self._verification_client
        try:
            if country_code:
                methods = client.get_mfa_channels(country_code)
                return tuple(
                    sorted(
                        {
                            *(
                                item
                                for item in self._verification_channels
                                if item in (1, 3)
                            ),
                            *(
                                method.channel
                                for method in methods
                                if method.supported and method.channel in PHONE_CHANNELS
                            ),
                        }
                    )
                )
            client.send_mfa_code(mfa_channel=channel)
            return ()
        finally:
            client.close_session()

    async def async_step_mfa_delivery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a bound verification method or explicitly discover phone methods."""
        if user_input is None:
            return self._show_delivery()
        if self._delivery_task is None or self._delivery_task.done():
            self._delivery_task = self.hass.async_create_task(
                self._async_submit_delivery(user_input), "EZVIZ MFA delivery"
            )
        return await asyncio.shield(self._delivery_task)

    async def _async_submit_delivery(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        channel = next(
            item
            for item in self._verification_channels
            if CHANNEL_NAMES[item] == user_input["method"]
        )
        country_code = user_input.get("country_code", "").strip()
        if country_code and (
            1 not in self._verification_channels
            or not country_code.isascii()
            or not country_code.isdigit()
            or len(country_code) > 6
        ):
            return self._show_delivery("invalid_country_code")

        use_existing_code = user_input.get("use_existing_code", False)
        if use_existing_code and not country_code:
            self._verification_channel = channel
            return self._show_verification(self._verification_step)
        try:
            channels = await asyncio.shield(
                create_executor_task(
                    self.hass, self._manage_delivery, channel, country_code
                )
            )
        except EzvizAuthVerificationCode as error:
            self._update_delivery_options(error)
            if self._verification_channels:
                return self._show_delivery(_login_error(error))
            return self._show_verification(self._verification_step, _login_error(error))
        except (PyEzvizError, OSError) as error:
            if country_code:
                self._verification_channels = tuple(
                    item for item in self._verification_channels if item in (1, 3)
                )
            return self._show_delivery(
                _login_error(error)
                if isinstance(error, EzvizLoginError)
                else "cannot_connect"
            )
        if country_code:
            self._verification_channels = channels
            return self._show_delivery()
        self._verification_channel = channel
        return self._show_verification(self._verification_step)

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
