"""Cached, allowlisted diagnostics without a second cloud/account request."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .runtime import EzvizConfigEntry

# Names, serial keys, URLs, network addresses and complete vendor mappings are
# intentionally excluded, rather than relying on an expanding credential denylist.
SAFE_FIELDS = {"device_category", "device_sub_category", "version", "status", "battery_level"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EzvizConfigEntry
) -> dict[str, Any]:
    """Do not expose full SDK tokens, push counters/keys, or camera image URLs."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    snapshot = coordinator.inventory_snapshot
    devices = coordinator.inventory_data if snapshot is not None else (coordinator.data or {})
    result: dict[str, Any] = {
        "poll_success": coordinator.last_update_success,
        "devices": [
            {
                k: v
                for k, v in device.items()
                if k in SAFE_FIELDS and (v is None or isinstance(v, (str, int, float, bool)))
            }
            for device in devices.values()
        ],
    }
    if snapshot is not None:
        result["inventory_revision"] = snapshot.revision
        result["device_health"] = [
            {
                "present": health.present,
                "current": health.current,
                "last_success": health.last_success.isoformat() if health.last_success else None,
                "error_type": health.error_type,
            }
            for health in snapshot.health.values()
        ]
    push = runtime.push
    if push is not None:
        result["push"] = await hass.async_add_executor_job(push.get_diagnostics)
    return result
