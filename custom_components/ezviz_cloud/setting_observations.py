"""Known property observations, distinct from SDK compatibility defaults."""

from collections.abc import Mapping
import json
from typing import TYPE_CHECKING, Any

from pyezvizapi.devices.features import optionals_mapping, supplement_light_params
from pyezvizapi.devices.lens import lens_defog_config
from pyezvizapi.devices.metadata import coerce_bool, coerce_int
from pyezvizapi.devices.tracking import ptz_master_slave_trace

if TYPE_CHECKING:
    from .select import EzvizSelect
    from .switch import EzvizSwitch


def _optional(data: dict[str, Any], key: str) -> Any:
    value = optionals_mapping(data).get(key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _port_value(data: dict[str, Any], ports: tuple[int, ...]) -> bool | None:
    # SDK normalization erases enabled-field presence/type. Require retained
    # canonical raw evidence; other shapes use bounded pending expiry.
    raw = data.get("NetworkSecurityProtection")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    entries = raw.get("portSecurityList") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        return None
    values = [
        bool(entry["enabled"])
        if type(entry.get("enabled")) in (bool, int) and entry["enabled"] in (0, 1)
        else None
        for entry in entries
        if isinstance(entry, Mapping) and coerce_int(entry.get("portNo")) in ports
    ]
    if True in values:
        return True
    return False if values and all(value is False for value in values) else None


def observed_switch(entity: EzvizSwitch) -> Any:
    """Missing/defaulted source fields cannot confirm either switch direction."""
    desc = entity.entity_description
    data = entity.coordinator.inventory_data[entity.serial]
    if desc.key == "motion_detection":
        return entity.observed_motion_detection()
    if desc.key in ("encrypted", "push_notify_alarm", "push_notify_call"):
        section, field = {
            "encrypted": ("STATUS", "isEncrypt"),
            "push_notify_alarm": ("NODISTURB", "alarmEnable"),
            "push_notify_call": ("NODISTURB", "callingEnable"),
        }[desc.key]
        source = data.get(section)
        raw = source.get(field) if isinstance(source, Mapping) else None
        if type(raw) not in (bool, int) or raw not in (0, 1):
            return None
    if desc.key == "offline_notify":
        source = data.get("deviceInfos")
        raw = source.get("offlineNotify") if isinstance(source, Mapping) else None
        if type(raw) not in (bool, int) or raw not in (0, 1):
            return None
    if desc.key.startswith("intelligent_app_"):
        # SDK2.4's public iterator erases enabled-field presence/type. Do not
        # confirm from bool coercion; bounded expiry restores observation.
        return None
    if desc.key == "OSD":
        if not isinstance(optionals_mapping(data).get("OSD"), (Mapping, list)):
            return None
    if desc.key == "intelligent_fill_light":
        return coerce_bool(supplement_light_params(data).get("enabled"))
    if desc.key == "port_security_client_mode":
        return _port_value(data, (80, 443, 8000))
    if desc.key == "port_security_50161":
        return _port_value(data, (50161,))
    return desc.value_fn(data)


def observed_select(entity: EzvizSelect) -> Any:
    """Validate known configuration before compatibility/defaulting getters."""
    desc = entity.entity_description
    data = entity.coordinator.inventory_data[entity.serial]
    if desc.key == "lens_defog":
        config: Any = lens_defog_config(data)
        enabled = config.get("enabled")
        if type(enabled) not in (bool, int) or enabled not in (0, 1):
            return None
        if enabled and str(config.get("defogMode")).lower() not in ("auto", "open"):
            return None
    elif desc.key == "ptz_linked_tracking_range":
        config = ptz_master_slave_trace(data).get("LinkedTrackingAdvancedCfg")
        value = (
            coerce_bool(config.get("trackingTakeoverEnabled"))
            if isinstance(config, Mapping)
            else None
        )
        return int(value) if value is not None else None
    elif desc.key == "image_style_setting":
        config = _optional(data, "display_mode")
        value = config.get("mode") if isinstance(config, Mapping) else config
        return value if type(value) is int and value in (1, 2, 3) else None
    elif desc.key == "blc_region":
        config = _optional(data, "inverse_mode")
        if type(config) is int and config in range(6):
            return config
        if not isinstance(config, Mapping) or type(config.get("enable")) is not int:
            return None
        if config["enable"] == 0:
            return 0
        if config["enable"] != 1 or config.get("position") not in range(1, 6):
            return None
    return desc.get_current_option(data)
