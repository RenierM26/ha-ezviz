"""Native camera options for selecting per-lens RTSP paths."""

from typing import TYPE_CHECKING, Any

import probatio
from pyezvizapi.devices.channels import CameraChannel, validate_channel_rtsp_path
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector

from .channels import (
    CONF_LENS_RTSP_PATHS,
    is_additional_camera_lens,
    lens_stream_presets,
    reported_lens_channels,
)
from .const import CONF_RTSP_STREAM_PATH, DEFAULT_RTSP_STREAM_PATH
from .credentials import CameraOptionsConflict, build_camera_options, camera_options

if TYPE_CHECKING:
    from .options_flow import EzvizOptionsFlowHandler

_LABELS = {
    "substream": "Substream (default)",
    "main_stream": "Main stream",
    "custom": "Custom path",
    "stills": "Stills only",
}


def _reported_lenses(
    flow: EzvizOptionsFlowHandler, serial: str
) -> tuple[CameraChannel, ...]:
    channels = reported_lens_channels(flow.coordinator, flow.config_entry, serial)
    return tuple(channel for channel in channels if is_additional_camera_lens(channel))


def _stream_fields(
    data: dict[str, Any], channels: tuple[CameraChannel, ...]
) -> dict[str, tuple[str, dict[str, str]]]:
    """Map the original camera and additional lenses to their existing options."""
    return {
        "lens_1": (
            CONF_RTSP_STREAM_PATH,
            {
                "substream": DEFAULT_RTSP_STREAM_PATH,
                "main_stream": "/Streaming/Channels/101",
            },
        ),
        **{
            f"lens_{channel.local_index}": (
                channel.key,
                lens_stream_presets(data, channel),
            )
            for channel in channels
        },
    }


def _saved_path(
    options: dict[str, Any], key: str, presets: dict[str, str]
) -> str | None:
    """Use the legacy primary path and keep secondary overrides independent."""
    if key == CONF_RTSP_STREAM_PATH:
        return options.get(key, DEFAULT_RTSP_STREAM_PATH)
    return options.get(CONF_LENS_RTSP_PATHS, {}).get(key, presets.get("substream"))


async def async_select_stream(
    flow: EzvizOptionsFlowHandler, serial: str, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Choose stream quality for each currently reported lens on this camera."""
    runtime = getattr(flow.config_entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.shutting_down:
        return flow.async_abort(reason="integration_unavailable")
    if serial not in flow.coordinator.data:
        return flow.async_abort(reason="no_cameras")
    channels = _reported_lenses(flow, serial)
    if user_input is None:
        flow.lens_baseline = camera_options(flow.config_entry, serial)
        flow.lens_modes = {}
    data = flow.coordinator.data[serial]
    fields = {}
    modes = {}
    errors = {}
    for key, (option, presets) in _stream_fields(data, channels).items():
        allowed = (*presets, "custom", "stills")
        path = _saved_path(flow.lens_baseline, option, presets)
        default = next(
            (mode for mode, value in presets.items() if value == path),
            "custom" if path is not None and (path or key == "lens_1") else "stills",
        )
        fields[probatio.Required(key, default=default)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=mode, label=_LABELS[mode])
                    for mode in allowed
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        if user_input is not None:
            mode = user_input.get(key)
            if mode in allowed:
                modes[option] = mode
            else:
                errors[key] = "invalid_channel"
    if user_input is not None and not errors:
        flow.lens_modes = modes
        return await flow.async_step_lens_stream_edit(
            None if "custom" in modes.values() else {}
        )
    return flow.async_show_form(
        step_id="lens_streams", data_schema=probatio.Schema(fields), errors=errors
    )


async def async_edit_stream(
    flow: EzvizOptionsFlowHandler, serial: str, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Show fields only for custom paths, and save all lens choices together."""
    runtime = getattr(flow.config_entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.shutting_down:
        return flow.async_abort(reason="integration_unavailable")
    channels = _reported_lenses(flow, serial)
    if serial not in flow.coordinator.data:
        return flow.async_abort(reason="no_cameras")
    data = flow.coordinator.data[serial]
    streams = _stream_fields(data, channels)
    if {option for option, _ in streams.values()} != flow.lens_modes.keys():
        return flow.async_abort(reason="no_camera_channels")
    paths = dict(flow.lens_baseline.get(CONF_LENS_RTSP_PATHS, {}))
    patch: dict[str, Any] = {}
    if channels:
        patch[CONF_LENS_RTSP_PATHS] = paths
    fields = {}
    errors = {}
    for key, (option, presets) in streams.items():
        mode = flow.lens_modes[option]
        saved = _saved_path(flow.lens_baseline, option, presets)
        if mode == "custom":
            fields[probatio.Required(key, default=saved or "")] = str
            if user_input is None:
                continue
            candidate = user_input.get(key)
            if key == "lens_1" and candidate == saved and saved is not None:
                # Keep existing custom paths verbatim, including legacy root paths.
                path: str | None = saved
            else:
                try:
                    path = validate_channel_rtsp_path(candidate)
                except PyEzvizError:
                    path = ""
                if not path:
                    errors[key] = "invalid_rtsp_path"
        elif mode == "stills":
            path = None if key == "lens_1" else ""
        elif mode in presets:
            path = presets[mode]
        else:
            return flow.async_abort(reason="no_camera_channels")
        if key == "lens_1":
            patch[CONF_RTSP_STREAM_PATH] = path
        else:
            paths[option] = path
    if user_input is not None and not errors:
        try:
            options = build_camera_options(
                flow.config_entry,
                serial,
                patch,
                expected=flow.lens_baseline,
            )
        except CameraOptionsConflict:
            result = await flow.async_step_lens_streams()
            result["errors"] = {"base": "options_changed"}
            return result
        return flow.async_create_entry(title="", data=options)
    return flow.async_show_form(
        step_id="lens_stream_edit", data_schema=probatio.Schema(fields), errors=errors
    )
