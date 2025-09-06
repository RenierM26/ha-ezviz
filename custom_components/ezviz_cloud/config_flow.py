"""Config + Options flow for EZVIZ Cloud integration (region-aware)."""

from __future__ import annotations

import logging
from typing import Any

from pyezvizapi.client import EzvizClient
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
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
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
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    REGION_CUSTOM,
    REGION_EU,
    REGION_RU,
    REGION_URLS,
)
from .coordinator import EzvizDataUpdateCoordinator

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
    cat = (cam_info.get("device_category") or "").lower()
    if "battery" in cat:
        return False
    return True


# -----------------------------------------------------------------------------
# Config Flow (cloud account)
# -----------------------------------------------------------------------------


class EzvizConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the cloud account config flow for EZVIZ."""

    VERSION = VERSION

    _reauth_entry: ConfigEntry | None = None
    _reauth_username: str | None = None
    _reauth_password: str | None = None
    _reauth_url: str | None = None

    _pending_user_username: str | None = None
    _pending_user_password: str | None = None
    _pending_user_url: str | None = None
    _pending_user_timeout: int = DEFAULT_TIMEOUT

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
            # Resolve host from region/custom
            try:
                api_url = _resolve_api_host(
                    user_input[CONF_REGION], user_input.get(CONF_URL)
                )
            except vol.Invalid:
                errors["base"] = "invalid_url"
                # fall through to re-render the form

            timeout = user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            if not errors:
                try:
                    client = EzvizClient(
                        username=username,
                        password=password,
                        api_url=api_url,
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
                        title=f"EZVIZ {username}",
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
                            "cameras": {},  # per-camera settings only
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

        username = getattr(self, "_pending_user_username", None)
        password = getattr(self, "_pending_user_password", None)
        api_url = getattr(self, "_pending_user_url", None)
        timeout = getattr(self, "_pending_user_timeout", DEFAULT_TIMEOUT)

        if not all([username, password, api_url]):
            # Missing context; restart
            return await self.async_step_user()

        if user_input is not None:
            sms_code = user_input.get("sms_code", "")

            try:
                client = EzvizClient(
                    username=username,
                    password=password,
                    api_url=api_url,
                    timeout=timeout,
                )
                token = await self.hass.async_add_executor_job(client.login, sms_code)

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"
            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover
                _LOGGER.exception("Unexpected error during initial MFA")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"EZVIZ {username}",
                    data={
                        CONF_TYPE: ATTR_TYPE_CLOUD,
                        CONF_SESSION_ID: token[CONF_SESSION_ID],
                        CONF_RF_SESSION_ID: token[CONF_RF_SESSION_ID],
                        CONF_URL: api_url,  # keep the chosen/normalized host
                        CONF_USER_ID: token["username"],
                    },
                    options={
                        CONF_TIMEOUT: timeout,
                        "cameras": {},
                    },
                )

        schema = vol.Schema({vol.Required("sms_code"): str})
        return self.async_show_form(
            step_id="user_mfa_confirm", data_schema=schema, errors=errors
        )

    # --------------------------
    # Reauth (cloud) + MFA
    # --------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for the existing cloud account."""
        entry: ConfigEntry | None = None
        for item in self._async_current_entries():
            if item.data.get(
                CONF_TYPE
            ) == ATTR_TYPE_CLOUD and item.unique_id == entry_data.get(CONF_USERNAME):
                entry = item
                break

        if entry is None:
            return self.async_abort(reason="already_configured")

        self._reauth_entry = entry
        self.context["title_placeholders"] = {"username": entry.title}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect password and try login; may require 2FA."""
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            username = self._reauth_entry.unique_id
            password = user_input[CONF_PASSWORD]
            api_url = self._reauth_entry.data[CONF_URL]
            timeout = self._reauth_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            try:
                client = EzvizClient(
                    username=username,
                    password=password,
                    api_url=api_url,
                    timeout=timeout,
                )
                token = await self.hass.async_add_executor_job(client.login)

            except EzvizAuthVerificationCode:
                self._reauth_username = username
                self._reauth_password = password
                self._reauth_url = api_url
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
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            sms_code = user_input.get("sms_code", "")
            username = self._reauth_username
            password = self._reauth_password
            api_url = self._reauth_url
            timeout = self._reauth_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            try:
                client = EzvizClient(
                    username=username,
                    password=password,
                    api_url=api_url,
                    timeout=timeout,
                )
                token = await self.hass.async_add_executor_job(client.login, sms_code)

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

    # Options flow entrypoint factory
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EzvizOptionsFlowHandler:
        """Get the options flow handler."""
        return EzvizOptionsFlowHandler(config_entry)


# -----------------------------------------------------------------------------
# Options Flow (cloud + cameras)
# -----------------------------------------------------------------------------


class EzvizOptionsFlowHandler(OptionsFlowWithConfigEntry):
    """Options flow to edit cloud and per-camera settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
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
        cameras = self.coordinator.data
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
        self, user_input: Any | None = None
    ) -> ConfigFlowResult:
        """Edit per-camera credentials; may branch to 2FA if EZVIZ requires it."""
        opts = dict(self.config_entry.options)
        per_cam = opts.get("cameras", {}).get(self._cam_serial, {})

        cam_info = self.coordinator.data.get(self._cam_serial, {})
        inferred_ip = cam_info["local_ip"]

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                ATTR_SERIAL: self._cam_serial,
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],  # may be "fetch_my_key"
                CONF_ENC_KEY: user_input[CONF_ENC_KEY],  # may be "fetch_my_key"
                CONF_RTSP_USES_VERIFICATION_CODE: user_input.get(
                    CONF_RTSP_USES_VERIFICATION_CODE,
                    per_cam.get(CONF_RTSP_USES_VERIFICATION_CODE, False),
                ),
                "ephemeral_test_rtsp": user_input.get(
                    "ephemeral_test_rtsp", False
                ),  # NOT stored
                CONF_FFMPEG_ARGUMENTS: user_input.get(
                    CONF_FFMPEG_ARGUMENTS,
                    per_cam.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS),
                ),
                # Pass IP only ephemerally for RTSP tests; never store it
                CONF_IP_ADDRESS: inferred_ip,
                "cloud_account_username": self.config_entry.unique_id,
            }

            try:
                resolved = await self._test_rtsp_credentials(data)

                cams_opts = opts.setdefault("cameras", {})
                cams_opts[self._cam_serial] = {
                    CONF_USERNAME: resolved[CONF_USERNAME],
                    # For older models, CONF_PASSWORD holds the sticker/verification code
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
                self._prefill = None  # clear one-shot prefill on success
                return self.async_create_entry(title="", data=opts)

            except EzvizAuthVerificationCode:
                self._pending = data
                return await self.async_step_camera_edit_2fa()

            except AuthTestResultFailed:
                errors["base"] = "rtsp_auth_failed"
            except DeviceException:
                errors["base"] = "device_exception"
            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error in camera_edit")
                return self.async_abort(reason="unknown")

        # Defaults for the form: prefer prefill (one-shot), then stored per-camera
        defd_username = (self._prefill or {}).get(CONF_USERNAME) or per_cam.get(
            CONF_USERNAME, DEFAULT_CAMERA_USERNAME
        )
        defd_password = (self._prefill or {}).get(CONF_PASSWORD) or per_cam.get(
            CONF_PASSWORD, "fetch_my_key"
        )
        defd_enc_key = (self._prefill or {}).get(CONF_ENC_KEY) or per_cam.get(
            CONF_ENC_KEY, "fetch_my_key"
        )
        defd_vc_mode = (
            (self._prefill or {}).get(CONF_RTSP_USES_VERIFICATION_CODE)
            if (self._prefill and CONF_RTSP_USES_VERIFICATION_CODE in self._prefill)
            else per_cam.get(CONF_RTSP_USES_VERIFICATION_CODE, False)
        )
        defd_ffmpeg = (self._prefill or {}).get(CONF_FFMPEG_ARGUMENTS) or per_cam.get(
            CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=defd_username): str,
                vol.Required(CONF_PASSWORD, default=defd_password): str,
                vol.Required(CONF_ENC_KEY, default=defd_enc_key): str,
                vol.Required(
                    CONF_RTSP_USES_VERIFICATION_CODE, default=defd_vc_mode
                ): bool,
                vol.Optional(
                    "ephemeral_test_rtsp", default=False
                ): bool,  # one-time; NOT stored
                vol.Optional(CONF_FFMPEG_ARGUMENTS, default=defd_ffmpeg): str,
            }
        )
        return self.async_show_form(
            step_id="camera_edit",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "serial": self._cam_serial,
                "ip_address": inferred_ip or "",
            },
        )

    async def async_step_camera_edit_2fa(
        self, user_input: Any | None = None
    ) -> ConfigFlowResult:
        """Collect one-time 2FA codes; if VC fails but ENC works, return to camera_edit prefilled."""
        if not self._pending:
            return await self.async_step_camera_edit()

        opts = dict(self.config_entry.options)
        per_cam = opts.get("cameras", {}).get(self._cam_serial, {})

        cam_info = self.coordinator.data.get(self._cam_serial, {})
        inferred_ip = cam_info["local_ip"]

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **self._pending,
                CONF_CAM_VERIFICATION_2FA_CODE: user_input.get(
                    CONF_CAM_VERIFICATION_2FA_CODE
                )
                or None,
                CONF_CAM_ENC_2FA_CODE: user_input.get(CONF_CAM_ENC_2FA_CODE) or None,
                # ensure IP is present for any RTSP test
                CONF_IP_ADDRESS: inferred_ip,
            }
            try:
                resolved = await self._test_rtsp_credentials(data)

                cams_opts = opts.setdefault("cameras", {})
                cams_opts[self._cam_serial] = {
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
                self._pending = None
                self._prefill = None
                return self.async_create_entry(title="", data=opts)

            except EzvizAuthVerificationCode:
                errors["base"] = "verification_required"

            except AuthTestResultFailed:
                errors["base"] = "rtsp_auth_failed"

            except DeviceException as err:
                # If VC path failed but ENC exists, bounce back to edit with ENC preselected
                has_enc = bool(
                    data.get(CONF_ENC_KEY) and data[CONF_ENC_KEY] != "fetch_my_key"
                )
                if has_enc:
                    _LOGGER.warning(
                        "VC fetch failed for %s; falling back to ENC and returning to edit: %s",
                        self._cam_serial,
                        err,
                    )
                    self._prefill = {
                        CONF_USERNAME: data.get(
                            CONF_USERNAME,
                            per_cam.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME),
                        ),
                        CONF_PASSWORD: per_cam.get(CONF_PASSWORD, "fetch_my_key"),
                        CONF_ENC_KEY: data[CONF_ENC_KEY],
                        CONF_RTSP_USES_VERIFICATION_CODE: False,
                        CONF_FFMPEG_ARGUMENTS: per_cam.get(
                            CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                        ),
                    }
                    self._pending = None
                    return await self.async_step_camera_edit()
                errors["base"] = "device_exception"

            except (InvalidURL, HTTPError, PyEzvizError):
                errors["base"] = "cannot_connect"

            except Exception:  # pragma: no cover
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

        # coordinator.data is serial -> cam_info
        cam_info = (self.coordinator.data or {}).get(data[ATTR_SERIAL], {})
        supports_rtsp_default = _infer_supports_rtsp_from_category(cam_info)

        try:
            # ENC key (used by newer cams & for last motion images)
            if data.get(CONF_ENC_KEY) == "fetch_my_key":
                data[CONF_ENC_KEY] = await self.hass.async_add_executor_job(
                    _get_cam_enc_key,
                    data,
                    ezviz_client,
                    data.get(CONF_CAM_ENC_2FA_CODE),  # optional one-time MFA
                )
                _LOGGER.info("Fetched encryption key for camera %s", data[ATTR_SERIAL])

            # Verification (sticker) code (older cam RTSP auth path)
            if data.get(CONF_PASSWORD) == "fetch_my_key":
                data[CONF_PASSWORD] = await self.hass.async_add_executor_job(
                    _get_cam_verification_code,
                    data,
                    ezviz_client,
                    data.get(CONF_CAM_VERIFICATION_2FA_CODE),  # optional one-time MFA
                )
                _LOGGER.info(
                    "Fetched verification code for camera %s", data[ATTR_SERIAL]
                )

            # Optional one-time RTSP test: only if user requested AND likely supported
            if data.get("ephemeral_test_rtsp") and supports_rtsp_default:
                await self.hass.async_add_executor_job(_wake_camera, data, ezviz_client)
                _LOGGER.debug(
                    "RTSP credentials verified for camera %s", data[ATTR_SERIAL]
                )

        except DeviceException:
            _LOGGER.warning(
                "Device error while preparing/testing %s", data.get(ATTR_SERIAL)
            )
            raise

        # Remove ephemeral values before returning
        data.pop(CONF_CAM_VERIFICATION_2FA_CODE, None)
        data.pop(CONF_CAM_ENC_2FA_CODE, None)
        data.pop("ephemeral_test_rtsp", None)

        return data
