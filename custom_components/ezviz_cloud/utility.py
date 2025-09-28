"""Integration-wide helper utilities."""

from __future__ import annotations

from typing import Any

__all__ = [
    "coerce_int",
    "device_category",
    "device_model",
    "network_type_value",
    "sd_card_capacity_gb",
    "support_ext_dict",
    "support_ext_has",
    "wifi_signal_value",
    "wifi_ssid_value",
]


def coerce_int(value: Any) -> int | None:
    """Best-effort coercion to int for mixed API payloads."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _wifi_section(camera_data: dict[str, Any]) -> dict[str, Any] | None:
    wifi_data = camera_data.get("WIFI")
    if isinstance(wifi_data, dict):
        return wifi_data
    return None


def wifi_ssid_value(camera_data: dict[str, Any]) -> str | None:
    """Return Wi-Fi SSID if provided."""
    wifi_data = _wifi_section(camera_data)
    if wifi_data is None:
        return None
    ssid = wifi_data.get("ssid")
    return ssid if isinstance(ssid, str) and ssid else None


def wifi_signal_value(camera_data: dict[str, Any]) -> int | None:
    """Return Wi-Fi signal strength percentage when an SSID is present."""
    wifi_data = _wifi_section(camera_data)
    if wifi_data is None:
        return None
    if not wifi_ssid_value(camera_data):
        return None
    return coerce_int(wifi_data.get("signal"))


def network_type_value(camera_data: dict[str, Any]) -> str | None:
    """Return network type (ethernet/wifi/other) if known."""
    wifi_data = _wifi_section(camera_data)
    if wifi_data is None:
        return None
    net_type = wifi_data.get("netType")
    if not isinstance(net_type, str) or not net_type:
        return None

    lowered = net_type.lower()
    if lowered == "wire":
        return "ethernet"
    if lowered == "wireless":
        return "wifi"
    return lowered


def sd_card_capacity_gb(camera_data: dict[str, Any]) -> float | None:
    """Return SD card capacity in gibibytes, if provided."""
    capacity = camera_data.get("diskCapacity")

    if isinstance(capacity, list):
        candidate = capacity[0] if capacity else None
    elif isinstance(capacity, str):
        candidate = capacity.split(",", 1)[0]
    else:
        candidate = capacity

    size_mb = coerce_int(candidate)
    if size_mb is None or size_mb <= 0:
        return None

    gb_value = size_mb / 1024
    return round(gb_value, 2)


def support_ext_dict(camera_data: dict[str, Any]) -> dict[str, Any]:
    """Return supportExt mapping if present."""
    support_ext = camera_data.get("supportExt")
    return support_ext if isinstance(support_ext, dict) else {}


def _support_ext_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    return {token.strip() for token in str(value).split(",") if token.strip()}


def support_ext_has(
    camera_data: dict[str, Any], key: str, expected_values: list[str] | None = None
) -> bool:
    """Check if supportExt contains the key (and optional values)."""
    ext = support_ext_dict(camera_data)
    raw = ext.get(key)
    if raw is None:
        return False
    if not expected_values:
        return True

    raw_str = str(raw).strip()
    normalized_expected = [value.strip() for value in expected_values if value.strip()]
    if raw_str in normalized_expected:
        return True

    have = _support_ext_tokens(raw)
    need_tokens: set[str] = set()
    for value in normalized_expected:
        need_tokens.update(_support_ext_tokens(value))

    if not need_tokens:
        return False

    return bool(have & need_tokens)


def device_category(camera_data: dict[str, Any]) -> str | None:
    """Return the device category if present."""
    category = camera_data.get("device_category")
    return str(category) if isinstance(category, str) else None


def device_model(camera_data: dict[str, Any]) -> str | None:
    """Return the device sub-category reported by the camera."""
    sub_category = camera_data.get("device_sub_category")
    return str(sub_category) if isinstance(sub_category, str) and sub_category else None
