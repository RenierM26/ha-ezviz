"""Shared HA availability from accepted SDK state and coordinator health."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator


def device_available(coordinator: EzvizDataUpdateCoordinator, serial: str) -> bool:
    """Never promote stale, unknown, failed-poll or closing state to available."""
    if (
        coordinator.shutting_down
        or not coordinator.last_update_success
        or not coordinator.device_is_current(serial)
    ):
        return False
    state = coordinator.device_state(serial)
    return state is not None and state.online is True
