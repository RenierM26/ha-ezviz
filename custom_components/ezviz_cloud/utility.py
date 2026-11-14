"""HA entity-description policy and compatibility imports for SDK helpers."""

from collections.abc import Callable, Iterable
from typing import Any

from pyezvizapi.devices.controls import (
    PortSecurityToggle,
    SupportExtView,
    coerce_bool,
    coerce_int,
    ensure_ptz_master_slave_trace,
    has_lens_defog,
    intelligent_app_available,
    intelligent_app_enabled,
    intelligent_app_method,
    intelligent_app_value_fn,
    iter_intelligent_apps,
    linked_tracking_takeover_enabled,
    network_type_value,
    ptz_master_slave_trace,
    sd_card_capacity_gb,
    set_lens_defog_option,
    set_linked_tracking_takeover,
    support_ext_dict,
    support_ext_has,
    wifi_signal_value,
    wifi_ssid_value,
)

__all__ = [
    "PortSecurityToggle",
    "SupportExtView",
    "coerce_bool",
    "coerce_int",
    "ensure_ptz_master_slave_trace",
    "has_lens_defog",
    "intelligent_app_available",
    "intelligent_app_enabled",
    "intelligent_app_method",
    "intelligent_app_value_fn",
    "iter_intelligent_apps",
    "linked_tracking_takeover_enabled",
    "network_type_value",
    "ptz_master_slave_trace",
    "sd_card_capacity_gb",
    "set_lens_defog_option",
    "set_linked_tracking_takeover",
    "support_ext_dict",
    "support_ext_has",
    "wifi_signal_value",
    "wifi_ssid_value",
]


def passes_description_gates(
    camera_data: dict[str, Any],
    *,
    supported_ext_keys: str | Iterable[str] | None = None,
    supported_ext_values: Iterable[str] | None = None,
    required_device_categories: tuple[str, ...] | None = None,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Evaluate common entity gating rules.

    The checks run in a fixed order and each gate is optional:
    1. supportExt presence/value (if keys provided)
    2. device_category membership (if categories provided)
    3. custom predicate (if supplied)
    """

    # Normalize supportExt keys to a tuple of strings.
    normalized_keys: tuple[str, ...]
    if supported_ext_keys is None:
        normalized_keys = ()
    elif isinstance(supported_ext_keys, str):
        normalized_keys = (supported_ext_keys,)
    else:
        normalized_keys = tuple(
            str(key) for key in supported_ext_keys if key is not None and str(key)
        )

    if normalized_keys:
        view = SupportExtView.from_camera_data(camera_data)
        normalized_values = SupportExtView.normalize_values(supported_ext_values)

        if not view.match_any(normalized_keys, normalized_values):
            return False

    if required_device_categories is not None:
        category = camera_data.get("device_category")
        if category not in required_device_categories:
            return False

    if predicate is not None:
        return bool(predicate(camera_data))

    return True
