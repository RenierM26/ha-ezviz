"""Integration-wide helper utilities."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyezvizapi.client import EzvizClient

__all__ = [
    "coerce_int",
    "day_night_mode_value",
    "day_night_sensitivity_value",
    "device_category",
    "device_icr_dss_config",
    "device_model",
    "display_mode_value",
    "has_osd_overlay",
    "intelligent_app_enabled",
    "intelligent_app_method",
    "intelligent_app_value_fn",
    "iter_intelligent_apps",
    "network_type_value",
    "night_vision_config",
    "night_vision_duration_value",
    "night_vision_luminance_value",
    "night_vision_mode_value",
    "night_vision_payload",
    "optionals_mapping",
    "resolve_channel",
    "sd_card_capacity_gb",
    "set_osd_overlay",
    "support_ext_dict",
    "support_ext_has",
    "wifi_signal_value",
    "wifi_ssid_value",
    "wrap_switch_method",
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


def optionals_mapping(camera_data: dict[str, Any]) -> dict[str, Any]:
    """Return decoded optionals mapping from the camera payload."""

    optionals: Any = (camera_data.get("statusInfo") or {}).get("optionals")
    if isinstance(optionals, str):
        try:
            optionals = json.loads(optionals)
        except (TypeError, ValueError):
            optionals = None

    if not isinstance(optionals, dict):
        optionals = camera_data.get("optionals")
        if isinstance(optionals, str):
            try:
                optionals = json.loads(optionals)
            except (TypeError, ValueError):
                optionals = None

    return optionals if isinstance(optionals, dict) else {}


def display_mode_value(camera_data: dict[str, Any]) -> int:
    """Return display mode value (1..3) from camera data."""

    optionals = optionals_mapping(camera_data)
    display_mode = optionals.get("display_mode")

    if isinstance(display_mode, str):
        try:
            display_mode = json.loads(display_mode)
        except (TypeError, ValueError):
            display_mode = None

    if isinstance(display_mode, dict):
        mode = display_mode.get("mode")
    else:
        mode = display_mode

    if isinstance(mode, int) and mode in (1, 2, 3):
        return mode

    return 1


def device_icr_dss_config(camera_data: dict[str, Any]) -> dict[str, Any]:
    """Decode and return the device_ICR_DSS configuration."""

    optionals = optionals_mapping(camera_data)
    icr = optionals.get("device_ICR_DSS")

    if isinstance(icr, str):
        try:
            icr = json.loads(icr)
        except (TypeError, ValueError):
            icr = None

    return icr if isinstance(icr, dict) else {}


def day_night_mode_value(camera_data: dict[str, Any]) -> int:
    """Return current day/night mode (0=auto,1=day,2=night)."""

    config = device_icr_dss_config(camera_data)
    mode = config.get("mode")
    if isinstance(mode, int) and mode in (0, 1, 2):
        return mode
    return 0


def day_night_sensitivity_value(camera_data: dict[str, Any]) -> int:
    """Return current day/night sensitivity value (1..3)."""

    config = device_icr_dss_config(camera_data)
    sensitivity = config.get("sensitivity")
    if isinstance(sensitivity, int) and sensitivity in (1, 2, 3):
        return sensitivity
    return 2


def resolve_channel(camera_data: dict[str, Any]) -> int:
    """Return the channel number to use for devconfig operations."""

    candidate = camera_data.get("channelNo") or camera_data.get("channel_no")
    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str) and candidate.isdigit():
        return int(candidate)
    return 1


def night_vision_config(camera_data: dict[str, Any]) -> dict[str, Any]:
    """Return decoded NightVision_Model configuration mapping."""

    optionals = optionals_mapping(camera_data)
    config: Any = optionals.get("NightVision_Model")
    if config is None:
        config = camera_data.get("NightVision_Model")

    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (TypeError, ValueError):
            config = None

    return config if isinstance(config, dict) else {}


def night_vision_mode_value(camera_data: dict[str, Any]) -> int:
    """Return current night vision mode (0=BW,1=colour,2=smart,5=super)."""

    config = night_vision_config(camera_data)
    mode_raw = config.get("graphicType")

    if isinstance(mode_raw, int):
        mode = mode_raw
    else:
        try:
            mode = int(mode_raw)
        except (TypeError, ValueError):
            mode = 0

    return mode if mode in (0, 1, 2, 5) else 0


def night_vision_luminance_value(camera_data: dict[str, Any]) -> int:
    """Return the configured night vision luminance (default 40)."""

    config = night_vision_config(camera_data)
    luminance = config.get("luminance")
    try:
        value = int(luminance)
    except (TypeError, ValueError):
        value = 40
    return max(0, value)


def night_vision_duration_value(camera_data: dict[str, Any]) -> int:
    """Return the configured smart night vision duration (default 60)."""

    config = night_vision_config(camera_data)
    duration = config.get("duration")
    try:
        value = int(duration)
    except (TypeError, ValueError):
        value = 60
    return value


def night_vision_payload(
    camera_data: dict[str, Any],
    *,
    mode: int | None = None,
    luminance: int | None = None,
    duration: int | None = None,
) -> dict[str, Any]:
    """Return a sanitized NightVision_Model payload for updates."""

    config = dict(night_vision_config(camera_data))

    resolved_mode = (
        int(mode)
        if mode is not None
        else int(config.get("graphicType") or night_vision_mode_value(camera_data))
    )
    config["graphicType"] = resolved_mode

    luminance_value = (
        int(luminance)
        if luminance is not None
        else night_vision_luminance_value(camera_data)
    )
    if resolved_mode == 1:
        config["luminance"] = (
            0 if luminance_value <= 0 else max(20, luminance_value)
        )
    elif resolved_mode == 2:
        config["luminance"] = max(
            20,
            luminance_value if luminance_value > 0 else 40,
        )
    else:
        config["luminance"] = max(0, luminance_value)

    duration_value = (
        int(duration)
        if duration is not None
        else night_vision_duration_value(camera_data)
    )
    if resolved_mode == 2:
        config["duration"] = max(15, min(120, duration_value))
    else:
        config.pop("duration", None)

    return config



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

def has_osd_overlay(camera_data: dict[str, Any]) -> bool:
    """Return True when the camera has an active OSD label."""

    osd_entries = camera_data.get("OSD")
    if isinstance(osd_entries, dict):
        osd_entries = [osd_entries]
    if not isinstance(osd_entries, list):
        return False

    for item in osd_entries:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return True
    return False


def _resolve_osd_channel(osd_entries: list[Any]) -> int:
    """Return the channel index to use for OSD operations."""

    if not osd_entries:
        return 1
    first = osd_entries[0]
    if not isinstance(first, dict):
        return 1
    channel_val = first.get("channel")
    if isinstance(channel_val, str) and channel_val.isdigit():
        return int(channel_val)
    if isinstance(channel_val, int):
        return channel_val
    return 1


def _resolve_osd_name(camera_data: dict[str, Any] | None, serial: str) -> str:
    """Return the name to use for the OSD label."""

    if isinstance(camera_data, dict):
        name = camera_data.get("name")
        if isinstance(name, str) and name:
            return name
        device_info = camera_data.get("deviceInfos")
        if isinstance(device_info, dict):
            alt_name = device_info.get("name")
            if isinstance(alt_name, str) and alt_name:
                return alt_name
    return serial


def set_osd_overlay(
    client: EzvizClient,
    serial: str,
    enable: int,
    camera_data: dict[str, Any] | None = None,
) -> bool:
    """Enable or disable the camera OSD label using coordinator data when available."""

    target_enabled = bool(enable)

    osd_entries: list[Any] = []
    if isinstance(camera_data, dict):
        osd = camera_data.get("OSD")
        if isinstance(osd, dict):
            osd_entries = [osd]
        elif isinstance(osd, list):
            osd_entries = list(osd)

    channel = _resolve_osd_channel(osd_entries)
    text = _resolve_osd_name(camera_data, serial) if target_enabled else ""

    client.set_camera_osd(serial, text, channel=channel)

    if not osd_entries:
        osd_entries = [{"name": text, "channel": str(channel)}]
    else:
        first = osd_entries[0]
        if isinstance(first, dict):
            first["name"] = text
            if "channel" not in first:
                first["channel"] = str(channel)
        else:
            osd_entries[0] = {"name": text, "channel": str(channel)}

    if isinstance(camera_data, dict):
        camera_data["OSD"] = osd_entries

    return True


def wrap_switch_method(
    fn: Callable[[EzvizClient, str, int], Any]
) -> Callable[[EzvizClient, str, int, dict[str, Any] | None], Any]:
    """Adapt a three-argument switch method to accept optional coordinator data."""

    def _wrapper(
        client: EzvizClient,
        serial: str,
        enable: int,
        camera_data: dict[str, Any] | None = None,
    ) -> Any:
        _ = camera_data
        return fn(client, serial, enable)

    return _wrapper
