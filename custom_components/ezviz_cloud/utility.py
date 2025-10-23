"""Integration-wide helper utilities."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import json
from typing import TYPE_CHECKING, Any

from pyezvizapi.feature import (
    lens_defog_config,
    normalize_port_security,
    port_security_config,
    port_security_has_port,
    port_security_port_enabled,
)

if TYPE_CHECKING:
    from pyezvizapi.client import EzvizClient


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
    if not isinstance(support_ext, dict):
        device_infos = camera_data.get("deviceInfos")
        if isinstance(device_infos, dict):
            support_ext = device_infos.get("supportExt")
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


def _support_ext_values_match(
    raw_value: Any, expected_values: tuple[str, ...]
) -> bool:
    """Return True if the raw supportExt value satisfies one of the candidates."""
    if not expected_values:
        return True

    raw_str = str(raw_value).strip()
    if not raw_str:
        return False

    raw_tokens = _support_ext_tokens(raw_value)
    if not raw_tokens:
        raw_tokens = {raw_str}

    for candidate in expected_values:
        if raw_str == candidate:
            return True

        candidate_tokens = _support_ext_tokens(candidate)
        if candidate_tokens and candidate_tokens <= raw_tokens:
            return True

    return False


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
        ext = support_ext_dict(camera_data)

        if supported_ext_values is not None:
            normalized_values = tuple(
                stripped
                for stripped in (str(raw).strip() for raw in supported_ext_values)
                if stripped
            )
        else:
            normalized_values = ()

        matched = False
        for key in normalized_keys:
            raw_value = ext.get(key)
            if raw_value is None:
                continue

            if normalized_values:
                if _support_ext_values_match(raw_value, normalized_values):
                    matched = True
                    break
            else:
                matched = True
                break

        if not matched:
            return False

    if required_device_categories is not None:
        category = device_category(camera_data)
        if category is None:
            category = camera_data.get("device_category")
        if category not in required_device_categories:
            return False

    if predicate is not None and not predicate(camera_data):
        return False

    return True


def device_category(camera_data: dict[str, Any]) -> str | None:
    """Return the device category if present."""
    category = camera_data.get("device_category")
    return str(category) if isinstance(category, str) else None


def device_model(camera_data: dict[str, Any]) -> str | None:
    """Return the device sub-category reported by the camera."""
    sub_category = camera_data.get("device_sub_category")
    return str(sub_category) if isinstance(sub_category, str) and sub_category else None


def has_lens_defog(camera_data: dict[str, Any]) -> bool:
    """Return True when this camera exposes a usable lens-defog configuration."""

    config = lens_defog_config(camera_data)
    if not isinstance(config, dict):
        return False

    mode = config.get("defogMode")
    return isinstance(mode, str) and bool(mode.strip())


def set_lens_defog_option(
    client: EzvizClient,
    serial: str,
    value: int,
    camera_data: dict[str, Any],
) -> None:
    """Persist lens-defog mode through the API and update cached data."""

    enabled, mode = client.set_lens_defog_mode(serial, value)

    config = lens_defog_config(camera_data)
    if isinstance(config, dict):
        config["enabled"] = enabled
        config["defogMode"] = mode


def _load_port_security_payload(client: EzvizClient, serial: str) -> dict[str, Any]:
    response = client.get_port_security(serial)
    value: dict[str, Any] = {}
    if isinstance(response, dict):
        value = normalize_port_security(response)

    if not value:
        camera_state = getattr(client, "_cameras", {}).get(serial)
        if isinstance(camera_state, dict):
            value = port_security_config(camera_state)

    if not value:
        value = {"portSecurityList": []}

    ports = value.get("portSecurityList")
    if not isinstance(ports, list):
        ports = []
    value["portSecurityList"] = ports

    if "enabled" not in value:
        value["enabled"] = True

    return value


def port_security_available_fn(port: int) -> Callable[[dict[str, Any]], bool]:
    """Return an availability predicate for a single port."""

    def _available(camera_data: dict[str, Any]) -> bool:
        return bool(port_security_has_port(camera_data, port))

    return _available


def port_security_value_fn(port: int) -> Callable[[dict[str, Any]], bool]:
    """Return a value extractor for a single secure-port flag."""

    def _value(camera_data: dict[str, Any]) -> bool:
        return bool(port_security_port_enabled(camera_data, port))

    return _value


def _set_port_security_port(
    client: EzvizClient, serial: str, port: int, enable: int
) -> None:
    value = _load_port_security_payload(client, serial)
    ports = value["portSecurityList"]

    for entry in ports:
        if isinstance(entry, dict) and coerce_int(entry.get("portNo")) == port:
            entry["enabled"] = bool(enable)
            break
    else:
        ports.append({"portNo": port, "enabled": bool(enable)})

    client.set_port_security(serial, value)


def port_security_method(port: int) -> Callable[[EzvizClient, str, int], None]:
    """Return a setter that toggles a single secure port."""

    def _method(client: EzvizClient, serial: str, enable: int) -> None:
        _set_port_security_port(client, serial, port, enable)

    return _method


def _set_port_security_ports(
    client: EzvizClient, serial: str, ports: tuple[int, ...], enable: int
) -> None:
    value = _load_port_security_payload(client, serial)
    entries = value["portSecurityList"]
    port_map: dict[int, dict[str, Any]] = {}

    for entry in entries:
        if isinstance(entry, dict):
            port_no = coerce_int(entry.get("portNo"))
            if port_no is not None:
                port_map[port_no] = entry

    for port in ports:
        entry = port_map.get(port)
        if entry is None:
            entry = {"portNo": port}
            entries.append(entry)
            port_map[port] = entry
        entry["enabled"] = bool(enable)

    client.set_port_security(serial, value)


def port_security_ports_available_fn(
    ports: tuple[int, ...],
) -> Callable[[dict[str, Any]], bool]:
    """Return an availability predicate for any port in the tuple."""

    def _available(camera_data: dict[str, Any]) -> bool:
        return any(port_security_has_port(camera_data, port) for port in ports)

    return _available


def port_security_ports_value_fn(
    ports: tuple[int, ...],
) -> Callable[[dict[str, Any]], bool]:
    """Return a value extractor that checks if any port is enabled."""

    def _value(camera_data: dict[str, Any]) -> bool:
        present = [port for port in ports if port_security_has_port(camera_data, port)]
        if not present:
            return False
        return any(port_security_port_enabled(camera_data, port) for port in present)

    return _value


def port_security_ports_method(
    ports: tuple[int, ...],
) -> Callable[[EzvizClient, str, int], None]:
    """Return a setter that toggles all provided secure ports."""

    def _method(client: EzvizClient, serial: str, enable: int) -> None:
        _set_port_security_ports(client, serial, ports, enable)

    return _method


def intelligent_app_value_fn(app_name: str) -> Callable[[dict[str, Any]], bool]:
    """Return a value extractor for an intelligent app."""

    def _value(camera_data: dict[str, Any]) -> bool:
        return intelligent_app_enabled(camera_data, app_name)

    return _value


def intelligent_app_method(app_name: str) -> Callable[[EzvizClient, str, int], bool]:
    """Return a setter callable for an intelligent app."""

    def _method(client: EzvizClient, serial: str, enable: int) -> bool:
        return bool(client.set_intelligent_app_state(serial, app_name, bool(enable)))

    return _method


def iter_intelligent_apps(camera_data: dict[str, Any]) -> Iterator[tuple[str, bool]]:
    """Yield (app_name, enabled) pairs for intelligent apps."""

    intelligent_app = None
    feature = camera_data.get("FEATURE_INFO")
    if isinstance(feature, dict):
        for group in feature.values():
            if not isinstance(group, dict):
                continue
            video_section = group.get("Video")
            if isinstance(video_section, dict) and "IntelligentAPP" in video_section:
                intelligent_app = video_section["IntelligentAPP"]
                break
            if "IntelligentAPP" in group:
                intelligent_app = group["IntelligentAPP"]
                break
    if intelligent_app is None:
        return

    if isinstance(intelligent_app, str):
        try:
            intelligent_app = json.loads(intelligent_app)
        except (TypeError, ValueError):
            intelligent_app = {}

    if not isinstance(intelligent_app, dict):
        return

    downloaded = intelligent_app.get("DownloadedAPP")
    if isinstance(downloaded, str):
        try:
            downloaded = json.loads(downloaded)
        except (TypeError, ValueError):
            downloaded = {}

    if not isinstance(downloaded, dict):
        return

    apps = downloaded.get("APP", [])
    if isinstance(apps, str):
        try:
            apps = json.loads(apps)
        except (TypeError, ValueError):
            apps = []

    if not isinstance(apps, list):
        return

    for app in apps:
        if not isinstance(app, dict):
            continue
        raw_app_id = app.get("APPID")
        if not isinstance(raw_app_id, str):
            continue
        base = raw_app_id.split("$:$", 1)[0]
        yield base, bool(app.get("enabled"))


def intelligent_app_enabled(camera_data: dict[str, Any], app_name: str) -> bool:
    """Return True if the given intelligent app is enabled."""

    for name, enabled in iter_intelligent_apps(camera_data) or []:
        if name == app_name:
            return enabled
    return False


def intelligent_app_available(camera_data: dict[str, Any], app_name: str) -> bool:
    """Return True if the intelligent app is present in the payload."""

    for name, _enabled in iter_intelligent_apps(camera_data) or []:
        if name == app_name:
            return True
    return False
