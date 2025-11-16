"""Config + Options flow for EZVIZ Cloud integration (region-aware)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)
from pyezvizapi.test_cam_rtsp import TestRTSPAuth
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import (
    HANDLERS,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .const import (
    # Camera fields
    ATTR_SERIAL,
    # Cloud entry
    ATTR_TYPE_CLOUD,
    CONF_CAM_ENC_2FA_CODE,
    # One-time (never persisted) 2FA codes for device-level fetches
    CONF_CAM_VERIFICATION_2FA_CODE,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,  # per-camera: RTSP path (legacy name)
    # Region support
    CONF_REGION,
    CONF_RF_SESSION_ID,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_SESSION_ID,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
    REGION_CUSTOM,
    REGION_EU,
    REGION_RU,
    REGION_URLS,
)
from .coordinator import EzvizDataUpdateCoordinator
from .utility import is_camera_device

_LOGGER = logging.getLogger(__name__)

VERSION = 4  # keep in sync with __init__.py TARGET_VERSION


# -----------------------------------------------------------------------------
# Low-level helpers (run in executor)
# -----------------------------------------------------------------------------


def _normalize_api_host(value: str) -> str:
    """Normalize an API host string (no scheme, no trailing slash/space)."""
    v = (value or "").strip()
    if v.startswith("http://"):
        v = v[7:]
    elif v.startswith("https://"):
        v = v[8:]
    return v.strip().strip("/")


def _resolve_api_host(region: str, custom_url: str | None) -> str:
    """Resolve concrete API host from region/custom selection."""
    if region == REGION_CUSTOM:
        host = _normalize_api_host(custom_url or "")
        if not host:
            raise vol.Invalid("invalid_url")
        return host
    return REGION_URLS[region]


def _get_cam_verification_code(
    data: dict, ezviz_client: EzvizClient, verification_code: int | None = None
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
    data: dict, ezviz_client: EzvizClient, enc_2fa_code: int | None = None
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


# -----------------------------------------------------------------------------
# Config Flow (cloud account)
# -----------------------------------------------------------------------------


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
    def async_get_options_flow(config_entry: ConfigEntry) -> EzvizOptionsFlowHandler:
        """Get the options flow handler."""
        return EzvizOptionsFlowHandler(config_entry)

    # --------------------------
    # Initial user setup (with MFA)
    # --------------------------

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
                api_url = _resolve_api_host(
                    user_input[CONF_REGION], user_input.get(CONF_URL)
                )
            except vol.Invalid:
                errors["base"] = "invalid_url"

            timeout = user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            if not errors:
                try:
                    client = EzvizClient(
                        account=username,
                        password=password,
                        url=api_url,
                        timeout=timeout,
                    )
                    # First attempt without SMS -> returns a token dict on success
                    token = await self.hass.async_add_executor_job(client.login)

                except EzvizAuthVerificationCode:
                    # Stash pending values; request SMS code
                    self._pending_user_username = username
                    self._pending_user_password = password
                    self._pending_user_url = api_url
                    self._pending_user_timeout = timeout
                    return await self.async_step_user_mfa_confirm()

                except (InvalidURL, HTTPError, PyEzvizError):
                    errors["base"] = "cannot_connect"

                except Exception:  # pragma: no cover - defensive
                    _LOGGER.exception("Unexpected error during EZVIZ login")
                    errors["base"] = "unknown"

                else:
                    # Persist token fields, not the raw password.
                    # Store the chosen API HOST (normalized), not whatever the token echoes.
                    return self.async_create_entry(
                        title=username,
                        data={
                            CONF_TYPE: ATTR_TYPE_CLOUD,
                            CONF_SESSION_ID: token[CONF_SESSION_ID],
                            CONF_RF_SESSION_ID: token[CONF_RF_SESSION_ID],
                            CONF_URL: api_url,  # host only, normalized
                            CONF_USER_ID: token[
                                "username"
                            ],  # ezviz internal user id (MQTT)
                        },
                        options={
                            CONF_TIMEOUT: timeout,
                            OPTIONS_KEY_CAMERAS: {},  # per-camera settings only
                        },
                    )

        # Show form
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default=REGION_EU): vol.In(
                    [REGION_EU, REGION_RU, REGION_CUSTOM]
                ),
                vol.Optional(CONF_URL): str,  # required only when region == custom
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
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
                )
                token = await self.hass.async_add_executor_job(client.login, sms_code)

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"
            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during initial MFA")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=self._pending_user_username,
                    data={
                        CONF_TYPE: ATTR_TYPE_CLOUD,
                        CONF_SESSION_ID: token[CONF_SESSION_ID],
                        CONF_RF_SESSION_ID: token[CONF_RF_SESSION_ID],
                        CONF_URL: self._pending_user_url,  # keep the chosen/normalized host
                        CONF_USER_ID: token["username"],
                    },
                    options={
                        CONF_TIMEOUT: self._pending_user_timeout,
                        OPTIONS_KEY_CAMERAS: {},
                    },
                )

        schema = vol.Schema({vol.Required("sms_code"): str})
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
        entry: ConfigEntry | None = None

        for item in self._async_current_entries():
            if item.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD:
                entry = await self.async_set_unique_id(item.unique_id)

        if entry is None:
            return self.async_abort(reason="unknown")

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
            self._reauth_url = self._reauth_entry.data[CONF_URL]
            self._reauth_timeout = self._reauth_entry.options[CONF_TIMEOUT]

            try:
                client = EzvizClient(
                    account=self._reauth_username,
                    password=self._reauth_password,
                    url=self._reauth_url,
                    timeout=self._reauth_timeout,
                )
                token = await self.hass.async_add_executor_job(client.login)

            except EzvizAuthVerificationCode:
                return await self.async_step_reauth_mfa()

            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                # Update only the rotating token fields; URL & user id are stable.
                new_data = {
                    **self._reauth_entry.data,
                    CONF_SESSION_ID: token[CONF_SESSION_ID],
                    CONF_RF_SESSION_ID: token[CONF_RF_SESSION_ID],
                }
                return self.async_update_reload_and_abort(
                    self._reauth_entry, data=new_data
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=self._reauth_entry.unique_id
                ): vol.In([self._reauth_entry.unique_id]),
                vol.Required(CONF_PASSWORD): str,
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
                )
                token = await self.hass.async_add_executor_job(
                    client.login, user_input["sms_code"]
                )

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"
            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover
                _LOGGER.exception("Unexpected error during reauth MFA")
                errors["base"] = "unknown"
            else:
                new_data = {
                    **self._reauth_entry.data,
                    CONF_SESSION_ID: token[CONF_SESSION_ID],
                    CONF_RF_SESSION_ID: token[CONF_RF_SESSION_ID],
                }
                return self.async_update_reload_and_abort(
                    self._reauth_entry, data=new_data
                )

        schema = vol.Schema({vol.Required("sms_code"): str})
        return self.async_show_form(
            step_id="reauth_mfa", data_schema=schema, errors=errors
        )


class EzvizOptionsFlowHandler(OptionsFlowWithReload):
    """Options flow to edit cloud and per-camera settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.coordinator: EzvizDataUpdateCoordinator
        self._cam_serial: str
        self._pending: dict | None = None  # hold values between edit -> 2FA
        self._prefill: dict | None = (
            None  # one-shot defaults when returning from 2FA fallback
        )

    async def async_step_init(self, user_input: Any | None = None) -> ConfigFlowResult:
        """Entry menu (bootstrap coordinator)."""
        self.coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id][
            DATA_COORDINATOR
        ]
        return self.async_show_menu(
            step_id="init", menu_options=["cloud", "camera_select"]
        )

    # ----- Cloud-level options -----

    async def async_step_cloud(self, user_input: Any | None = None) -> ConfigFlowResult:
        """Edit cloud account options (timeout only)."""
        opts = dict(self.config_entry.options)

        if user_input is not None:
            opts[CONF_TIMEOUT] = user_input[CONF_TIMEOUT]
            # No cloud-level CONF_FFMPEG_ARGUMENTS anymore (per-camera only)
            return self.async_create_entry(title="", data=opts)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TIMEOUT, default=opts.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): int,
            }
        )
        return self.async_show_form(step_id="cloud", data_schema=schema)

    # ----- Camera selection -----

    async def async_step_camera_select(
        self, user_input: Any | None = None
    ) -> ConfigFlowResult:
        """Choose which camera to configure.

        coordinator.data is a mapping: serial -> { name, ip, device_category, ... }
        """
        cameras = {
            serial: info
            for serial, info in (self.coordinator.data or {}).items()
            if is_camera_device(info)
        }
        if not cameras:
            return self.async_abort(reason="no_cameras")

        choices = {
            serial: f"{info.get('name', 'Camera')} ({serial})"
            for serial, info in cameras.items()
        }

        if user_input is not None:
            self._cam_serial = user_input["serial"]
            return await self.async_step_camera_edit()

        return self.async_show_form(
            step_id="camera_select",
            data_schema=vol.Schema({vol.Required("serial"): vol.In(choices)}),
        )

    # ----- Camera edit (may require 2FA) -----

    async def async_step_camera_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit per-camera credentials; branch to 2FA if EZVIZ requires it."""
        base_opts = dict(self.config_entry.options or {})
        per_cam = (base_opts.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(
            self._cam_serial, {}
        )

        cam_info = self.coordinator.data[self._cam_serial]
        inferred_ip = cam_info["local_ip"]
        test_rtsp_default = _infer_supports_rtsp_from_category(cam_info)

        errors: dict[str, str] = {}

        def _make_prefill(
            src: dict[str, Any] | None, ui: dict[str, Any] | None
        ) -> dict[str, Any]:
            src = src or {}
            ui = ui or {}
            return {
                CONF_USERNAME: src.get(
                    CONF_USERNAME,
                    ui.get(
                        CONF_USERNAME,
                        per_cam.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME),
                    ),
                ),
                CONF_PASSWORD: src.get(
                    CONF_PASSWORD, ui.get(CONF_PASSWORD, DEFAULT_FETCH_MY_KEY)
                ),
                CONF_ENC_KEY: src.get(
                    CONF_ENC_KEY,
                    ui.get(
                        CONF_ENC_KEY, per_cam.get(CONF_ENC_KEY, DEFAULT_FETCH_MY_KEY)
                    ),
                ),
                CONF_RTSP_USES_VERIFICATION_CODE: src.get(
                    CONF_RTSP_USES_VERIFICATION_CODE,
                    ui.get(
                        CONF_RTSP_USES_VERIFICATION_CODE,
                        per_cam.get(CONF_RTSP_USES_VERIFICATION_CODE, False),
                    ),
                ),
                CONF_FFMPEG_ARGUMENTS: src.get(
                    CONF_FFMPEG_ARGUMENTS,
                    ui.get(
                        CONF_FFMPEG_ARGUMENTS,
                        per_cam.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS),
                    ),
                ),
                "ephemeral_test_rtsp": ui.get("ephemeral_test_rtsp", test_rtsp_default),
            }

        if user_input is not None:
            payload = {
                **user_input,
                CONF_IP_ADDRESS: inferred_ip,
                "cloud_account_username": self.config_entry.unique_id,
                ATTR_SERIAL: self._cam_serial,
            }

            try:
                resolved = await self._test_rtsp_credentials(payload)

                # Success → write fresh options dict
                cams_old = base_opts.get(OPTIONS_KEY_CAMERAS, {}) or {}
                cams_new = dict(cams_old)
                cams_new[self._cam_serial] = {
                    CONF_USERNAME: resolved[CONF_USERNAME],
                    CONF_PASSWORD: resolved[CONF_PASSWORD],
                    CONF_ENC_KEY: resolved[CONF_ENC_KEY],
                    CONF_RTSP_USES_VERIFICATION_CODE: resolved[
                        CONF_RTSP_USES_VERIFICATION_CODE
                    ],
                    CONF_FFMPEG_ARGUMENTS: resolved.get(
                        CONF_FFMPEG_ARGUMENTS,
                        cams_old.get(self._cam_serial, {}).get(
                            CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                        ),
                    ),
                }
                new_opts = dict(base_opts)
                new_opts[OPTIONS_KEY_CAMERAS] = cams_new

                self._pending = None
                self._prefill = None
                return self.async_create_entry(title="", data=new_opts)

            except EzvizAuthVerificationCode:
                self._pending = payload
                self._prefill = _make_prefill(user_input, None)
                return await self.async_step_camera_edit_2fa()

            except AuthTestResultFailed as err:
                errors["base"] = "rtsp_auth_failed"
                self._prefill = _make_prefill(getattr(err, "data", None), user_input)

            except DeviceException as err:
                errors["base"] = "device_exception"
                self._prefill = _make_prefill(getattr(err, "data", None), user_input)

            except (InvalidURL, HTTPError, PyEzvizError) as err:
                errors["base"] = "cannot_connect"
                self._prefill = _make_prefill(getattr(err, "data", None), user_input)

            except Exception:
                _LOGGER.exception("Unexpected error in camera_edit")
                return self.async_abort(reason="unknown")

        pf = self._prefill or _make_prefill(per_cam, None)
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=pf[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD, default=pf[CONF_PASSWORD]): str,
                vol.Required(CONF_ENC_KEY, default=pf[CONF_ENC_KEY]): str,
                vol.Required(
                    CONF_RTSP_USES_VERIFICATION_CODE,
                    default=pf[CONF_RTSP_USES_VERIFICATION_CODE],
                ): bool,
                vol.Required(
                    "ephemeral_test_rtsp", default=pf["ephemeral_test_rtsp"]
                ): bool,
                vol.Optional(
                    CONF_FFMPEG_ARGUMENTS, default=pf[CONF_FFMPEG_ARGUMENTS]
                ): str,
            }
        )
        return self.async_show_form(
            step_id="camera_edit",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "serial": self._cam_serial,
                "ip_address": inferred_ip,
            },
        )

    async def async_step_camera_edit_2fa(
        self, user_input: Any | None = None
    ) -> ConfigFlowResult:
        """Collect one-time 2FA codes; if VC fails but ENC works, return to camera_edit prefilled."""
        if not self._pending:
            return await self.async_step_camera_edit()

        opts = dict(self.config_entry.options or {})
        per_cam = (opts.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(self._cam_serial, {})

        cam_info = (self.coordinator.data or {}).get(self._cam_serial, {}) or {}
        inferred_ip = cam_info.get("local_ip") or ""

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **self._pending,
                CONF_CAM_VERIFICATION_2FA_CODE: user_input.get(
                    CONF_CAM_VERIFICATION_2FA_CODE
                )
                or None,
                CONF_CAM_ENC_2FA_CODE: user_input.get(CONF_CAM_ENC_2FA_CODE) or None,
                CONF_IP_ADDRESS: inferred_ip,
            }
            try:
                resolved = await self._test_rtsp_credentials(data)

                base_opts = dict(self.config_entry.options or {})
                cams_old = base_opts.get(OPTIONS_KEY_CAMERAS, {}) or {}
                cams_new = dict(cams_old)

                cams_new[self._cam_serial] = {
                    CONF_USERNAME: resolved[CONF_USERNAME],
                    CONF_PASSWORD: resolved[CONF_PASSWORD],
                    CONF_ENC_KEY: resolved[CONF_ENC_KEY],
                    CONF_RTSP_USES_VERIFICATION_CODE: resolved[
                        CONF_RTSP_USES_VERIFICATION_CODE
                    ],
                    CONF_FFMPEG_ARGUMENTS: resolved.get(
                        CONF_FFMPEG_ARGUMENTS,
                        per_cam.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS),
                    ),
                }

                new_opts = dict(base_opts)
                new_opts[OPTIONS_KEY_CAMERAS] = cams_new

                # Clear ephemerals
                self._pending = None
                self._prefill = None

                return self.async_create_entry(title="", data=new_opts)

            except EzvizAuthVerificationCode:
                # Still needs a code
                errors["base"] = "verification_required"

            except AuthTestResultFailed as err:
                errors["base"] = "rtsp_auth_failed"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit()

            except DeviceException as err:
                # If VC path failed but ENC exists, bounce back to edit with ENC preselected
                errors["base"] = "device_exception"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit()

            except (InvalidURL, HTTPError, PyEzvizError) as err:
                errors["base"] = "cannot_connect"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit()

            except Exception:
                _LOGGER.exception("Unexpected error in camera_edit_2fa")
                return self.async_abort(reason="unknown")

        schema = vol.Schema(
            {
                vol.Optional(CONF_CAM_VERIFICATION_2FA_CODE, default=""): str,
                vol.Optional(CONF_CAM_ENC_2FA_CODE, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="camera_edit_2fa",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "serial": self._cam_serial,
                "ip_address": inferred_ip or "",
            },
        )

    # --------------------------
    # Shared validator for both camera steps
    # --------------------------

    async def _test_rtsp_credentials(self, data: dict) -> dict:
        """Resolve/fetch creds; run RTSP wake+probe only when explicitly requested.

        - Fetches ENC key / verification code when the user typed 'fetch_my_key'.
        - Accepts optional one-time 2FA codes (never persisted).
        - If 'ephemeral_test_rtsp' is True and the camera likely supports RTSP,
          calls _wake_camera(), which in turn calls _test_camera_rtsp_creds().
        - Drops any one-time codes / flags from the returned dict.
        """
        ezviz_client: EzvizClient = self.coordinator.ezviz_client

        try:
            # ENC key (used by newer cams & for last motion images)
            if data.get(CONF_ENC_KEY) == DEFAULT_FETCH_MY_KEY:
                data[CONF_ENC_KEY] = await self.hass.async_add_executor_job(
                    _get_cam_enc_key,
                    data,
                    ezviz_client,
                    data.get(CONF_CAM_ENC_2FA_CODE),  # optional one-time MFA
                )
                _LOGGER.info("Fetched encryption key for camera %s", data[ATTR_SERIAL])

            # Verification (sticker) code (older cam RTSP auth)
            if data.get(CONF_PASSWORD) == DEFAULT_FETCH_MY_KEY:
                data[CONF_PASSWORD] = await self.hass.async_add_executor_job(
                    _get_cam_verification_code,
                    data,
                    ezviz_client,
                    data.get(CONF_CAM_VERIFICATION_2FA_CODE),  # optional one-time MFA
                )
                _LOGGER.info(
                    "Fetched verification code for camera %s", data[ATTR_SERIAL]
                )

            # Optional one-time RTSP test: only if user requested
            if data.get("ephemeral_test_rtsp"):
                await self.hass.async_add_executor_job(_wake_camera, data, ezviz_client)
                _LOGGER.debug(
                    "RTSP credentials verified for camera %s", data[ATTR_SERIAL]
                )

        except EzvizAuthVerificationCode:
            _LOGGER.warning(
                "EZVIZ requested 2FA code while preparing/testing %s",
                data.get(ATTR_SERIAL),
            )
            raise

        except DeviceException as err:
            _LOGGER.warning(
                "Device error while preparing/testing %s", data.get(ATTR_SERIAL)
            )
            # Attach whatever data we have so far for prefill
            e = DeviceException(f"EZVIZ Device error: {err}")
            e.data = data
            raise e from err

        except AuthTestResultFailed as err:
            _LOGGER.warning("RTSP auth failed for camera %s", data.get(ATTR_SERIAL))
            e = AuthTestResultFailed("RTSP DESCRIBE auth test failed")
            e.data = data
            raise e from err

        except PyEzvizError as err:
            _LOGGER.warning(
                "EZVIZ API error while preparing/testing %s", data.get(ATTR_SERIAL)
            )
            e = PyEzvizError(
                f"EZVIZ API error, could be account permission for retrieving key: {err}"
            )
            e.data = data
            raise e from err

        # Remove ephemeral values before returning
        data.pop(CONF_CAM_VERIFICATION_2FA_CODE, None)
        data.pop(CONF_CAM_ENC_2FA_CODE, None)
        data.pop("ephemeral_test_rtsp", None)
        data.pop(CONF_IP_ADDRESS, None)
        data.pop("cloud_account_username")

        return data


# Register flow handler for this custom domain (avoids mypy complaints about __init_subclass__)
HANDLERS.register(DOMAIN)(EzvizConfigFlow)
