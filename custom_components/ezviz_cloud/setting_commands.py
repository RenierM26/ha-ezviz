"""Prepare compound writes from detached observations and accepted siblings."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING, Any

from pyezvizapi.devices.audio import (
    alarm_voice_volume_getter,
    microphone_volume_getter,
    volume_config,
)
from pyezvizapi.devices.features import (
    day_night_sensitivity_value,
    device_icr_dss_config,
    night_vision_config,
    night_vision_mode_value,
    night_vision_payload,
    optionals_mapping,
    resolve_channel,
)

from .setting_writes import SettingCommand

if TYPE_CHECKING:
    from .number import EzvizNumber
    from .select import EzvizSelect

NIGHT_MODE_KEYS = {
    "night_vision_model",
    "smart_night_vision_model",
    "smart_night_vision_model_battery",
    "super_night_vision_model",
}
_NIGHT_FIELDS = ("graphicType", "luminance", "duration")


def _voice_config(data: dict[str, Any]) -> dict[str, Any]:
    return volume_config(data, "CustomVoice_Volume") or {}


def _write_config(data: dict[str, Any], group: str, config: dict[str, Any]) -> None:
    """Honor SDK optional-data precedence on this detached command copy."""
    options = optionals_mapping(data)
    options[group] = deepcopy(config)
    data["optionals"] = options
    for key in ("statusInfo", "STATUS"):
        if isinstance(status := data.get(key), Mapping):
            data[key] = {**status, "optionals": deepcopy(options)}


def _project(
    data: dict[str, Any],
    group: str,
    getter: Callable,
    field: str,
    payload: dict[str, Any],
) -> None:
    config = getter(data)
    if group == "CustomVoice_Volume":
        known_value = (
            alarm_voice_volume_getter if field == "volume" else microphone_volume_getter
        )(data)
        if known_value is None:
            # An accepted field cannot supply its own missing SDK validation input,
            # even when sibling/unknown fields keep the config nonempty.
            return
    config = config or {}
    if field in payload:
        config[field] = payload[field]
    else:
        config.pop(field, None)
    _write_config(data, group, config)


def _config_projections(
    group: str, getter: Callable, payload: dict[str, Any], fields: tuple[str, ...]
) -> tuple[tuple[str, Callable[[dict[str, Any]], None]], ...]:
    return tuple(
        (
            f"{group}:{field}",
            partial(_project, group=group, getter=getter, field=field, payload=payload),
        )
        for field in fields
    )


def prepare_select(entity: EzvizSelect, value: int) -> SettingCommand:
    """Read accepted siblings only once the account write lock is acquired."""
    desc = entity.entity_description
    data = entity.coordinator.settings.command_data(entity.serial, entity.data)
    client, serial = entity.coordinator.ezviz_client, entity.serial
    if desc.key in ("day_night_mode", "day_night_sensitivity"):
        payload = {
            "mode": value if desc.key == "day_night_mode" else 0,
            "sensitivity": day_night_sensitivity_value(data)
            if desc.key == "day_night_mode"
            else value,
        }
        return SettingCommand(
            client.set_dev_config_kv,
            (serial, resolve_channel(data), "device_ICR_DSS", payload),
            lambda: _config_projections(
                "device_ICR_DSS",
                device_icr_dss_config,
                payload,
                ("mode", "sensitivity"),
            ),
        )
    if desc.key in NIGHT_MODE_KEYS:
        payload = night_vision_payload(data, mode=value)
        return SettingCommand(
            client.set_dev_config_kv,
            (serial, resolve_channel(data), "NightVision_Model", payload),
            lambda: _config_projections(
                "NightVision_Model",
                night_vision_config,
                payload,
                _NIGHT_FIELDS,
            ),
        )
    return SettingCommand(
        desc.set_current_option,
        (client, serial, value, data),
    )


def _number_projections(
    entity: EzvizNumber, value: float, data: dict[str, Any]
) -> tuple[tuple[str, Callable[[dict[str, Any]], None]], ...]:
    desc = entity.entity_description
    if desc.key in ("night_vision_luminance", "night_vision_duration"):
        kwargs = {
            "luminance" if desc.key.endswith("luminance") else "duration": round(value)
        }
        payload = night_vision_payload(
            data, mode=night_vision_mode_value(data), **kwargs
        )
        return _config_projections(
            "NightVision_Model",
            night_vision_config,
            payload,
            _NIGHT_FIELDS,
        )
    if desc.key in ("alarm_voice_volume", "microphone_volume"):
        field = "volume" if desc.key == "alarm_voice_volume" else "microphone_volume"
        payload = {
            **_voice_config(data),
            field: round(value)
            if field == "volume"
            else max(0, min(100, round(value))),
        }
        return _config_projections(
            "CustomVoice_Volume",
            _voice_config,
            payload,
            ("volume", "microphone_volume"),
        )
    return ()


def prepare_number(entity: EzvizNumber, value: float) -> SettingCommand:
    """Keep SDK validation and retain only compound sibling command context."""
    data = entity.coordinator.settings.command_data(entity.serial, entity.data)
    return SettingCommand(
        entity.entity_description.set_value,
        (entity.coordinator.ezviz_client, entity.serial, value, data),
        lambda: _number_projections(entity, value, data),
    )
