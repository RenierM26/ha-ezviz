"""Cached, allowlisted diagnostics without account requests or private payloads."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .availability import device_available
from .const import (
    CONF_ENC_KEY,
    CONF_RTSP_USES_VERIFICATION_CODE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
)
from .runtime import EzvizConfigEntry

# Values are deliberately selected, never copied from entry.as_dict()/SDK payloads.
SAFE_FIELDS = {
    "device_category",
    "device_sub_category",
    "version",
    "status",
    "battery_level",
}
CAPABILITY_FIELDS = (
    "status",
    "battery_level",
    "supported_channels",
    "encrypted",
    "alarm_notify",
    "PIR_Status",
    "push_notify_alarm",
    "push_notify_call",
    "is_on",
    "brightness",
    "color_temperature",
    "alarm_light_luminance",
    "door_status",
    "water_leak_status",
    "siren_work_state",
    "zigbee_signal",
    "power_mode",
    "upgrade_available",
    "upgrade_in_progress",
    "upgrade_status",
    "upgrade_phase",
)


def _age(now: datetime, timestamp: datetime | None) -> float | None:
    """Report freshness, not a household activity timeline."""
    if timestamp is None or timestamp.tzinfo is None:
        return None
    return round(max(0, (now - timestamp).total_seconds()), 3)


def _scalar(value: Any) -> Any:
    """Allow only finite JSON scalars in reviewed device fields."""
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _pending(items: Any) -> int | None:
    return sum(not item.done() for item in items) if items is not None else None


def _device_record(
    serial: str,
    label: str,
    coordinator: Any,
    options: Mapping,
    entities: list,
    now: datetime,
) -> dict[str, Any]:
    snapshot = coordinator.inventory_snapshot
    state = snapshot.devices.get(serial) if snapshot else None
    health = snapshot.health.get(serial) if snapshot else None
    # Camera push updates live in data; other device families use inventory_data.
    values = (coordinator.data or {}).get(
        serial, coordinator.inventory_data.get(serial, {})
    )
    capabilities = state.capabilities if state else None
    camera_options = options.get(OPTIONS_KEY_CAMERAS, {}).get(serial, {})
    status = values.get("status")
    return {
        "device": label,
        "kind": state.kind if state else None,
        "details": {
            key: _scalar(values[key]) for key in sorted(SAFE_FIELDS) if key in values
        },
        "online": status in (1, "1")
        if type(status) in (int, str) and status in (1, "1", 2, "2")
        else None,
        "available": device_available(coordinator, serial),
        "health": {
            "present": health.present if health else None,
            "current": health.current if health else None,
            "last_success_age_seconds": _age(now, health.last_success)
            if health
            else None,
            "error_type": health.error_type if health else None,
        },
        "capabilities": {
            # An absent observation is unknown, not unsupported.
            "observed": {
                key: capabilities.has(key) if capabilities else None
                for key in CAPABILITY_FIELDS
            },
            "verified_binary_state_available": {
                key: True
                if capabilities
                and type(capabilities.verified_booleans.get(key)) is bool
                else None
                for key in ("door_open", "water_leak")
            },
        },
        "configuration": {
            "rtsp_username_configured": bool(camera_options.get("username")),
            "rtsp_password_configured": bool(camera_options.get("password")),
            "encryption_key_configured": bool(camera_options.get(CONF_ENC_KEY)),
            "rtsp_uses_verification_code": camera_options.get(
                CONF_RTSP_USES_VERIFICATION_CODE, False
            )
            is True,
        },
        "entities": {
            "enabled": sum(entity.disabled_by is None for entity in entities),
            "disabled": sum(entity.disabled_by is not None for entity in entities),
        },
    }


async def _async_diagnostics(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    device: dr.AnyDeviceEntry | None = None,
) -> dict[str, Any]:
    """Detach the event-loop snapshot before awaiting any SDK locks."""
    runtime = getattr(entry, "runtime_data", None)
    result: dict[str, Any] = {
        "schema_version": 2,
        "entry": {
            "state": entry.state.value,
            "version": entry.version,
            "minor_version": entry.minor_version,
        },
        "runtime_available": runtime is not None,
        "devices": [],
    }
    if runtime is None:
        return result
    coordinator = runtime.coordinator
    snapshot = coordinator.inventory_snapshot
    now = dt_util.utcnow()
    result["polling"] = {
        "success": coordinator.last_update_success,
        "last_exception_type": type(coordinator.last_exception).__name__
        if getattr(coordinator, "last_exception", None)
        else None,
        "snapshot_age_seconds": _age(now, snapshot.received_at) if snapshot else None,
        "snapshot_revision": snapshot.revision if snapshot else None,
        "interval_seconds": coordinator.update_interval.total_seconds()
        if getattr(coordinator, "update_interval", None)
        else None,
        "timeout_seconds": _scalar(
            getattr(
                coordinator,
                "_api_timeout",
                entry.options.get("timeout", DEFAULT_TIMEOUT),
            )
        ),
        "in_flight": getattr(coordinator, "poll_in_flight", False),
    }
    result["lifecycle"] = {
        "shutting_down": getattr(coordinator, "shutting_down", None),
        "pending_actions": _pending(getattr(coordinator, "_action_futures", None)),
        "pending_setup_workers": _pending(getattr(runtime, "_workers", None)),
        "cleanup_complete": getattr(runtime, "cleanup_complete", None),
        "cleanup_error_type": getattr(runtime, "cleanup_error_type", None),
    }
    known = set(coordinator.inventory_data) | set(coordinator.data or {})
    if snapshot:
        known.update(snapshot.devices)
        known.update(snapshot.health)  # Retain missing/stale device health too.
    registry = dr.async_get(hass)
    registered = dr.async_entries_for_config_entry(registry, entry.entry_id)
    registered_ids = {item.id for item in registered}
    device_ids_by_serial: dict[str, set[str]] = defaultdict(set)
    for registered_device in registered:
        for domain, identifier in registered_device.identifiers:
            if domain == DOMAIN:
                device_ids_by_serial[identifier].add(registered_device.id)
    known.update(device_ids_by_serial)
    entity_registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    entities_by_device: dict[str | None, list] = defaultdict(list)
    for entity in entities:
        entities_by_device[entity.device_id].append(entity)
    # Export-local aliases; no serial hashes, names, registry IDs or entity IDs.
    for index, serial in enumerate(sorted(known), 1):
        if device is not None and (
            device.id not in registered_ids
            or (DOMAIN, serial) not in device.identifiers
        ):
            continue
        result["devices"].append(
            _device_record(
                serial,
                f"device_{index}",
                coordinator,
                entry.options,
                [
                    item
                    for device_id in device_ids_by_serial.get(serial, ())
                    for item in entities_by_device.get(device_id, ())
                ],
                now,
            )
        )
    push = runtime.push
    if push is None:
        result["push"] = {"state": "not_started"}
    else:
        try:
            result["push"] = await hass.async_add_executor_job(push.get_diagnostics)
        except Exception as error:  # noqa: BLE001 - Report diagnostics failure without losing cached data.
            result["push"] = {
                "state": "diagnostics_failed",
                "error_type": type(error).__name__,
            }
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
) -> dict[str, Any]:
    """Return a cached account snapshot safe to attach to a public issue."""
    return await _async_diagnostics(hass, entry)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    device: dr.AnyDeviceEntry,
) -> dict[str, Any]:
    """Return shared health context and only the selected device's record."""
    return await _async_diagnostics(hass, entry, device)
