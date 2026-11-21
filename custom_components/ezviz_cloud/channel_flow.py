"""Native camera options for selecting per-lens RTSP paths."""

from typing import TYPE_CHECKING, Any

import probatio
from pyezvizapi.devices.channels import (
    CameraChannel,
    camera_channels,
    validate_channel_rtsp_path,
)
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector

from .channels import (
    CONF_LENS_RTSP_PATHS,
    is_additional_camera_lens,
    lens_stream_presets,
)
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
    channels = camera_channels(flow.coordinator.data.get(serial, {}))
    if len(channels) < 2:
        return ()
    return tuple(channel for channel in channels if is_additional_camera_lens(channel))


def _field(channel: CameraChannel) -> str:
    return f"lens_{channel.local_index}"


async def async_select_stream(
    flow: EzvizOptionsFlowHandler, serial: str, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Choose stream quality for each currently reported lens on this camera."""
    runtime = getattr(flow.config_entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.shutting_down:
        return flow.async_abort(reason="integration_unavailable")
    channels = _reported_lenses(flow, serial)
    if not channels:
        return flow.async_abort(reason="no_camera_channels")
    if user_input is None:
        flow.lens_baseline = camera_options(flow.config_entry, serial)
        flow.lens_modes = {}
    data = flow.coordinator.data[serial]
    paths = flow.lens_baseline.get(CONF_LENS_RTSP_PATHS, {})
    fields = {}
    modes = {}
    errors = {}
    for channel in channels:
        key = _field(channel)
        presets = lens_stream_presets(data, channel)
        allowed = (*presets, "custom", "stills")
        if channel.key in paths:
            path = paths[channel.key]
            default = next(
                (mode for mode, value in presets.items() if value == path),
                "custom" if path else "stills",
            )
        else:
            default = "substream" if presets else "stills"
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
                modes[channel.key] = mode
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
    if {channel.key for channel in channels} != flow.lens_modes.keys():
        return flow.async_abort(reason="no_camera_channels")
    data = flow.coordinator.data[serial]
    paths = dict(flow.lens_baseline.get(CONF_LENS_RTSP_PATHS, {}))
    fields = {}
    errors = {}
    for channel in channels:
        key = _field(channel)
        mode = flow.lens_modes[channel.key]
        presets = lens_stream_presets(data, channel)
        if mode == "custom":
            fields[probatio.Required(key, default=paths.get(channel.key, ""))] = str
            if user_input is not None:
                try:
                    path = validate_channel_rtsp_path(user_input.get(key))
                except PyEzvizError:
                    path = ""
                if path:
                    paths[channel.key] = path
                else:
                    errors[key] = "invalid_rtsp_path"
        elif mode == "stills":
            paths[channel.key] = ""
        elif mode in presets:
            paths[channel.key] = presets[mode]
        else:
            return flow.async_abort(reason="no_camera_channels")
    if user_input is not None and not errors:
        try:
            options = build_camera_options(
                flow.config_entry,
                serial,
                {CONF_LENS_RTSP_PATHS: paths},
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
