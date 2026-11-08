"""Pure availability policy; no device enums are invented here."""

from collections.abc import Mapping
from typing import Any


def device_available(coordinator: Any, serial: str) -> bool:
    """Honor polling failures and missing state, preserving known legacy codes."""
    if not coordinator.last_update_success:
        return False
    current = getattr(coordinator, "device_is_current", None)
    if callable(current) and not current(serial):
        return False
    lookup = getattr(coordinator, "device_data", None)
    data = {serial: lookup(serial)} if callable(lookup) else coordinator.data
    if not isinstance(data, Mapping):
        return False
    device = data.get(serial)
    if not isinstance(device, Mapping):
        return False
    status = device.get("status")
    if status is None or isinstance(status, bool):
        return False
    # Native device connectivity uses 1 for online and 2 for offline.
    # Anything else is unknown, not evidence that the device is online.
    return isinstance(status, (int, str)) and status in (1, "1")
