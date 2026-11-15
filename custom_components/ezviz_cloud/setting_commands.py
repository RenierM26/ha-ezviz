"""Prepare effective setting targets and compound writes from detached context."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING, Any

from pyezvizapi.devices.audio import (
    alarm_voice_volume_getter,
    alarm_volume_config,
    alarm_volume_getter,
    alarm_volume_max,
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

from .confirmation import SettingCommand, SettingEffect
from .setting_observations import observed_select

if TYPE_CHECKING:
    from .number import EzvizNumber
    from .select import EzvizSelect

NIGHT_MODE_KEYS = {
    "night_vision_model",
    "smart_night_vision_model",
    "smart_night_vision_model_battery",
    "super_night_vision_model",
}
_NIGHT_FIELDS = {
    "graphicType": "select:night_vision_mode",
    "luminance": "number:night_vision_luminance",
    "duration": "number:night_vision_duration",
}


def select_setting_key(key: str) -> str:
    """Aliases of one SDK property share pending state, not public IDs."""
    return "select:night_vision_mode" if key in NIGHT_MODE_KEYS else f"select:{key}"


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
    if group == "ChimeMusic" and alarm_volume_getter(data) is None:
        return
    if group == "CustomVoice_Volume":
        known_value = (
            alarm_voice_volume_getter if field == "volume" else microphone_volume_getter
        )(data)
        if known_value is None:
            # A pending field cannot supply its own missing SDK validation input,
            # even when sibling/unknown fields keep the config nonempty.
            return
    config = config or {}
    if field in payload:
        config[field] = payload[field]
    else:
        config.pop(field, None)
    _write_config(data, group, config)


def _known_field(data: dict[str, Any], group: str, getter: Callable, field: str) -> Any:
    config = getter(data)
    if group == "CustomVoice_Volume":
        return (
            alarm_voice_volume_getter if field == "volume" else microphone_volume_getter
        )(data)
    raw = config.get(field)
    if group == "device_ICR_DSS":
        allowed = (0, 1, 2) if field == "mode" else (1, 2, 3)
        return raw if type(raw) is int and raw in allowed else None
    try:
        value = int(raw) if raw is not None and not isinstance(raw, bool) else None
    except TypeError, ValueError, OverflowError:
        value = None
    if group == "NightVision_Model":
        if field == "graphicType":
            return value if value in (0, 1, 2, 5) else None
        if field == "duration" and field not in config:
            mode = config.get("graphicType")
            return 60 if mode in (0, 1, 5) else None
    return value


def _observed_field(entity: Any, group: str, getter: Callable, field: str) -> Any:
    return _known_field(
        entity.coordinator.inventory_data[entity.serial], group, getter, field
    )


def _config_effects(
    entity: Any,
    group: str,
    getter: Callable,
    payload: dict[str, Any],
    fields: dict[str, str],
    metadata: dict[str, Any],
) -> tuple[SettingEffect, ...]:
    # Keep validation metadata from the actual prepared command, not a later poll.
    context = deepcopy(metadata)
    _write_config(context, group, payload)
    effects = []
    for field, key in fields.items():
        value = _known_field(context, group, getter, field)
        if value is None:
            continue
        effects.append(
            SettingEffect(
                key,
                value,
                partial(_observed_field, entity, group, getter, field),
                partial(
                    _project, group=group, getter=getter, field=field, payload=payload
                ),
            )
        )
    return tuple(effects)


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
            lambda: _config_effects(
                entity,
                "device_ICR_DSS",
                device_icr_dss_config,
                payload,
                {
                    "mode": "select:day_night_mode",
                    "sensitivity": "select:day_night_sensitivity",
                },
                data,
            ),
        )
    if desc.key in NIGHT_MODE_KEYS:
        payload = night_vision_payload(data, mode=value)
        return SettingCommand(
            client.set_dev_config_kv,
            (serial, resolve_channel(data), "NightVision_Model", payload),
            lambda: _config_effects(
                entity,
                "NightVision_Model",
                night_vision_config,
                payload,
                _NIGHT_FIELDS,
                data,
            ),
        )
    return SettingCommand(
        desc.set_current_option,
        (client, serial, value, data),
        lambda: (
            SettingEffect(
                select_setting_key(desc.key),
                value,
                lambda: observed_select(entity),
            ),
        ),
    )


def _number_effects(
    entity: EzvizNumber, value: float, data: dict[str, Any]
) -> tuple[SettingEffect, ...]:
    desc = entity.entity_description
    if desc.key in ("night_vision_luminance", "night_vision_duration"):
        kwargs = {
            "luminance" if desc.key.endswith("luminance") else "duration": round(value)
        }
        payload = night_vision_payload(
            data, mode=night_vision_mode_value(data), **kwargs
        )
        return _config_effects(
            entity,
            "NightVision_Model",
            night_vision_config,
            payload,
            _NIGHT_FIELDS,
            data,
        )
    if desc.key in ("alarm_voice_volume", "microphone_volume"):
        field = "volume" if desc.key == "alarm_voice_volume" else "microphone_volume"
        payload = {
            **_voice_config(data),
            field: round(value)
            if field == "volume"
            else max(0, min(100, round(value))),
        }
        return _config_effects(
            entity,
            "CustomVoice_Volume",
            _voice_config,
            payload,
            {
                "volume": "number:alarm_voice_volume",
                "microphone_volume": "number:microphone_volume",
            },
            data,
        )
    project = None
    if desc.key == "alarm_volume":
        value = max(0, min(int(alarm_volume_max(data)), round(value)))
        payload = {**(alarm_volume_config(data) or {}), "volume": value}

        def project(target: dict[str, Any]) -> None:
            _project(target, "ChimeMusic", alarm_volume_config, "volume", payload)
    elif desc.key in (
        "detection_sensibility",
        "algorithm_param_0_1",
        "algorithm_param_3_1",
        "algorithm_param_4_1",
    ):
        value = int(value)
    return (
        SettingEffect(
            f"number:{desc.key}",
            value,
            lambda: desc.get_value(entity.coordinator.inventory_data[entity.serial]),
            project,
        ),
    )


def prepare_number(entity: EzvizNumber, value: float) -> SettingCommand:
    """Retain raw SDK validation; normalize effective effects after acceptance."""
    data = entity.coordinator.settings.command_data(entity.serial, entity.data)
    return SettingCommand(
        entity.entity_description.set_value,
        (entity.coordinator.ezviz_client, entity.serial, value, data),
        lambda: _number_effects(entity, value, data),
    )
