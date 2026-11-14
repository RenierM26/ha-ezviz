"""Options UI for live account settings and per-camera credentials."""

import logging
from typing import TYPE_CHECKING, Any

import probatio
from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)

from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .camera_flow import (
    CameraCredentialFetchError,
    _infer_supports_rtsp_from_category,
    async_resolve_camera_credentials,
)
from .const import (
    ATTR_SERIAL,
    CONF_CAM_ENC_2FA_CODE,
    CONF_CAM_VERIFICATION_2FA_CODE,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RTSP_USES_VERIFICATION_CODE,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    OPTIONS_KEY_CAMERAS,
)
from .credentials import CameraOptionsConflict, build_camera_options, camera_options
from .flow_validation import validate_timeout

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator
    from .runtime import EzvizConfigEntry

_LOGGER = logging.getLogger(__name__)


class EzvizOptionsFlowHandler(OptionsFlowWithReload):
    """Options flow to edit cloud and per-camera settings."""

    def __init__(self, config_entry: EzvizConfigEntry) -> None:
        """Initialize options flow."""
        self._cam_serial: str
        self._camera_baseline: dict[str, Any] = {}
        self._pending: dict | None = None  # hold values between edit -> 2FA
        self._prefill: dict | None = (
            None  # one-shot defaults when returning from 2FA fallback
        )

    async def async_step_init(self, user_input: Any | None = None) -> ConfigFlowResult:
        """Entry menu (bootstrap coordinator)."""
        return self.async_show_menu(
            step_id="init", menu_options=["cloud", "camera_select"]
        )

    @property
    def coordinator(self) -> EzvizDataUpdateCoordinator:
        """Resolve the live owner for each step, not a stale captured coordinator."""
        return self.config_entry.runtime_data.coordinator

    # ----- Cloud-level options -----

    async def async_step_cloud(self, user_input: Any | None = None) -> ConfigFlowResult:
        """Edit cloud account options (timeout only)."""
        opts = dict(self.config_entry.options)
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                opts[CONF_TIMEOUT] = validate_timeout(user_input[CONF_TIMEOUT])
            except probatio.Invalid:
                errors[CONF_TIMEOUT] = "invalid_timeout"
            else:
                # No cloud-level CONF_FFMPEG_ARGUMENTS anymore (per-camera only)
                return self.async_create_entry(title="", data=opts)

        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_TIMEOUT, default=opts.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): probatio.All(int, probatio.Range(min=1, max=300)),
            }
        )
        return self.async_show_form(step_id="cloud", data_schema=schema, errors=errors)

    # ----- Camera selection -----

    async def async_step_camera_select(
        self, user_input: Any | None = None
    ) -> ConfigFlowResult:
        """Choose which camera to configure.

        coordinator.data is a mapping: serial -> { name, ip, device_category, ... }
        """
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None or runtime.coordinator.shutting_down:
            return self.async_abort(reason="integration_unavailable")
        cameras = self.coordinator.data
        if not cameras:
            return self.async_abort(reason="no_cameras")

        choices = {
            serial: f"{info.get('name', 'Camera')} ({serial})"
            for serial, info in cameras.items()
        }

        if user_input is not None:
            self._cam_serial = user_input["serial"]
            self._camera_baseline = camera_options(self.config_entry, self._cam_serial)
            return await self.async_step_camera_edit()

        return self.async_show_form(
            step_id="camera_select",
            data_schema=probatio.Schema(
                {probatio.Required("serial"): probatio.In(choices)}
            ),
        )

    # ----- Camera edit (may require 2FA) -----

    async def async_step_camera_edit(
        self,
        user_input: dict[str, Any] | None = None,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Edit per-camera credentials; branch to 2FA if EZVIZ requires it."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None or runtime.coordinator.shutting_down:
            return self.async_abort(reason="integration_unavailable")
        if self._cam_serial not in (self.coordinator.data or {}):
            return self.async_abort(reason="no_cameras")

        base_opts = dict(self.config_entry.options or {})
        per_cam = (base_opts.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(
            self._cam_serial, {}
        )

        cam_info = self.coordinator.data[self._cam_serial]
        inferred_ip = cam_info["local_ip"]
        test_rtsp_default = _infer_supports_rtsp_from_category(cam_info)

        errors = errors or {}

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

                return self._camera_options_result(resolved)

            except CameraCredentialFetchError as err:
                errors[err.field] = err.error_key
                self._prefill = _make_prefill(err.data, user_input)

            except CameraOptionsConflict:
                self._camera_baseline = camera_options(
                    self.config_entry, self._cam_serial
                )
                self._pending = None
                self._prefill = None
                return await self.async_step_camera_edit(
                    errors={"base": "options_changed"}
                )

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
        schema = probatio.Schema(
            {
                probatio.Required(CONF_USERNAME, default=pf[CONF_USERNAME]): str,
                probatio.Required(CONF_PASSWORD, default=pf[CONF_PASSWORD]): str,
                probatio.Required(CONF_ENC_KEY, default=pf[CONF_ENC_KEY]): str,
                probatio.Required(
                    CONF_RTSP_USES_VERIFICATION_CODE,
                    default=pf[CONF_RTSP_USES_VERIFICATION_CODE],
                ): bool,
                probatio.Required(
                    "ephemeral_test_rtsp", default=pf["ephemeral_test_rtsp"]
                ): bool,
                probatio.Optional(
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
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None or runtime.coordinator.shutting_down:
            return self.async_abort(reason="integration_unavailable")
        if self._cam_serial not in (self.coordinator.data or {}):
            return self.async_abort(reason="no_cameras")

        if not self._pending:
            return await self.async_step_camera_edit()

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

                return self._camera_options_result(resolved)

            except CameraCredentialFetchError as err:
                self._pending = None
                self._prefill = err.data
                return await self.async_step_camera_edit(
                    errors={err.field: err.error_key}
                )

            except CameraOptionsConflict:
                self._camera_baseline = camera_options(
                    self.config_entry, self._cam_serial
                )
                self._pending = None
                self._prefill = None
                return await self.async_step_camera_edit(
                    errors={"base": "options_changed"}
                )

            except EzvizAuthVerificationCode:
                # Still needs a code
                errors["base"] = "verification_required"

            except AuthTestResultFailed as err:
                errors["base"] = "rtsp_auth_failed"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit(errors=errors)

            except DeviceException as err:
                # If VC path failed but ENC exists, bounce back to edit with ENC preselected
                errors["base"] = "device_exception"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit(errors=errors)

            except (InvalidURL, HTTPError, PyEzvizError) as err:
                errors["base"] = "cannot_connect"
                self._prefill = getattr(err, "data", None)
                return await self.async_step_camera_edit(errors=errors)

            except Exception:
                _LOGGER.exception("Unexpected error in camera_edit_2fa")
                return self.async_abort(reason="unknown")

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_CAM_VERIFICATION_2FA_CODE, default=""): str,
                probatio.Optional(CONF_CAM_ENC_2FA_CODE, default=""): str,
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

    @callback
    def _camera_options_result(self, resolved: dict[str, Any]) -> ConfigFlowResult:
        """One fresh-read patch/compare path for normal and MFA completion."""
        patch = {
            CONF_USERNAME: resolved[CONF_USERNAME],
            CONF_PASSWORD: resolved[CONF_PASSWORD],
            CONF_ENC_KEY: resolved[CONF_ENC_KEY],
            CONF_RTSP_USES_VERIFICATION_CODE: resolved[
                CONF_RTSP_USES_VERIFICATION_CODE
            ],
            CONF_FFMPEG_ARGUMENTS: resolved.get(
                CONF_FFMPEG_ARGUMENTS,
                self._camera_baseline.get(
                    CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                ),
            ),
        }
        options = build_camera_options(
            self.config_entry, self._cam_serial, patch, expected=self._camera_baseline
        )
        self._pending = None
        self._prefill = None
        return self.async_create_entry(title="", data=options)

    async def _test_rtsp_credentials(self, data: dict) -> dict:
        """Delegate resolution while retaining the live coordinator and edit baseline."""
        return await async_resolve_camera_credentials(
            self.coordinator, data, self._camera_baseline, self._cam_serial
        )
