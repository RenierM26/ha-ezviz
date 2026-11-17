"""Native options for explicitly configured lens streams; no network probes."""

from typing import TYPE_CHECKING, Any

import probatio
from pyezvizapi.devices.channels import (
    camera_channels,
    resolve_camera_channel,
    validate_channel_rtsp_path,
)
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector

from .channels import CONF_LENS_RTSP_PATHS
from .credentials import CameraOptionsConflict, build_camera_options, camera_options

if TYPE_CHECKING:
    from .options_flow import EzvizOptionsFlowHandler


async def async_select_stream(
    flow: EzvizOptionsFlowHandler, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Select a reported channel using labels, without exposing resource IDs."""
    runtime = getattr(flow.config_entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.shutting_down:
        return flow.async_abort(reason="integration_unavailable")
    targets = {}
    for serial, data in flow.coordinator.data.items():
        channels = camera_channels(data)
        if len(channels) > 1:
            for channel in channels:
                targets[f"{serial}|{channel.key}"] = (serial, channel)
    if not targets:
        return flow.async_abort(reason="no_camera_channels")
    if user_input is not None and user_input.get("channel") in targets:
        serial, channel = targets[user_input["channel"]]
        flow.lens_target = (serial, channel.key)
        flow.lens_baseline = camera_options(flow.config_entry, serial)
        return await flow.async_step_lens_stream_edit()
    return flow.async_show_form(
        step_id="lens_streams",
        data_schema=probatio.Schema(
            {
                probatio.Required("channel"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=key,
                                label=f"{flow.coordinator.data[serial]['name']} — Lens {channel.local_index}",
                            )
                            for key, (serial, channel) in targets.items()
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        ),
        errors={"channel": "invalid_channel"} if user_input is not None else {},
    )


async def async_edit_stream(
    flow: EzvizOptionsFlowHandler, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Commit a path with existing conflict protection and native reload ownership."""
    runtime = getattr(flow.config_entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.shutting_down:
        return flow.async_abort(reason="integration_unavailable")
    if flow.lens_target is None:
        return await flow.async_step_lens_streams()
    serial, key = flow.lens_target
    try:
        channel = resolve_camera_channel(flow.coordinator.data.get(serial, {}), key)
    except PyEzvizError:
        return flow.async_abort(reason="no_camera_channels")
    errors = {}
    if user_input is not None:
        try:
            path = validate_channel_rtsp_path(user_input.get("rtsp_path", ""))
            paths = dict(flow.lens_baseline.get(CONF_LENS_RTSP_PATHS, {}))
            if path:
                paths[key] = path
            else:
                paths.pop(key, None)
            options = build_camera_options(
                flow.config_entry,
                serial,
                {CONF_LENS_RTSP_PATHS: paths},
                expected=flow.lens_baseline,
            )
            return flow.async_create_entry(title="", data=options)
        except CameraOptionsConflict:
            flow.lens_baseline = camera_options(flow.config_entry, serial)
            errors["base"] = "options_changed"
        except PyEzvizError:
            errors["rtsp_path"] = "invalid_rtsp_path"
    return flow.async_show_form(
        step_id="lens_stream_edit",
        data_schema=probatio.Schema(
            {
                probatio.Optional(
                    "rtsp_path",
                    default=flow.lens_baseline.get(CONF_LENS_RTSP_PATHS, {}).get(
                        key, ""
                    ),
                ): str
            }
        ),
        errors=errors,
        description_placeholders={"channel": channel.local_index},
    )
