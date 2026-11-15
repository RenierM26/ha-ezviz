"""Entry-owned, bounded intercom event deduplication and subscriptions."""

from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from pyezvizapi.devices.intercom import device_category
from pyezvizapi.devices.intercom_events import IntercomEvent, normalize_intercom_event
from pyezvizapi.devices.snapshot import DeviceSnapshot

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

MAX_SEEN_EVENTS = 1024
MAX_EVENT_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(seconds=30)


def event_zones(inventory: dict[str, dict[str, Any]]) -> dict[str, tzinfo]:
    """Resolve evidenced IANA zones in the poll executor, never a push callback."""
    zones: dict[str, tzinfo] = {}
    for serial, data in inventory.items():
        value = data.get("cam_timezone")
        if not isinstance(value, str) or not 0 < len(value) <= 128:
            continue
        try:
            zone = dt_util.get_time_zone(value)
        except ValueError:
            # Invalid paths/absolute names must not acquire the host's timezone.
            continue
        if zone is not None:
            zones[serial] = zone
    return zones


class IntercomEvents:
    """No polling, tasks or timers of its own; all callbacks run on HA's loop."""

    def __init__(self, coordinator: EzvizDataUpdateCoordinator) -> None:
        """Start without listeners or historical event replay."""
        self.coordinator = coordinator
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._seeded: set[str] = set()
        self._zones: dict[str, tzinfo] = {}
        self._listeners: dict[str, list[Callable[[IntercomEvent], None]]] = {}

    @callback
    def subscribe(
        self, serial: str, listener: Callable[[IntercomEvent], None]
    ) -> Callable[[], None]:
        """Own a removable subscription per entity, including registry renames."""
        listeners = self._listeners.setdefault(serial, [])
        listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                self._listeners.pop(serial, None)

        return remove

    @callback
    def snapshot(
        self, snapshot: DeviceSnapshot, zones: dict[str, tzinfo] | None = None
    ) -> None:
        """Seed each device's first successful history window without firing it."""
        if zones is not None:
            self._zones = zones
        if snapshot.intercom_events_current is not True:
            return
        serials = {
            serial
            for serial, state in snapshot.devices.items()
            if device_category(state.values) == "BDoorBell"
            and (health := snapshot.health.get(serial)) is not None
            and health.present
            and health.current
        }
        first = serials - self._seeded
        for event in reversed(snapshot.intercom_events):
            self._accept(event, emit=event.serial not in first)
        self._seeded.update(serials)

    @callback
    def push(self, payload: Mapping[str, Any]) -> IntercomEvent | None:
        """Normalize picture-less pushes without assigning naive times to UTC."""
        ext = payload.get("ext")
        serial = ext.get("device_serial") if isinstance(ext, Mapping) else None
        zone = self._zones.get(serial) if isinstance(serial, str) else None
        if event := normalize_intercom_event(payload, zone=zone):
            self._accept(event, emit=True)
            return event
        return None

    @callback
    def _accept(self, event: IntercomEvent, *, emit: bool) -> None:
        if self.coordinator.shutting_down:
            return
        state = self.coordinator.device_state(event.serial)
        snapshot = self.coordinator.inventory_snapshot
        health = snapshot.health.get(event.serial) if snapshot else None
        if (
            state is None
            or device_category(state.values) != "BDoorBell"
            or health is None
            or not health.present
        ):
            return
        if emit and event.occurred_at is None:
            # A later history record may supply the timestamp missing from push.
            return
        key = (event.serial, event.event_id)
        if key in self._seen:
            return
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN_EVENTS:
            self._seen.popitem(last=False)
        if not emit or event.occurred_at is None:
            return
        age = dt_util.utcnow() - event.occurred_at
        if age > MAX_EVENT_AGE or age < -MAX_CLOCK_SKEW:
            return
        for listener in tuple(self._listeners.get(event.serial, ())):
            listener(event)

    @callback
    def close(self) -> None:
        """Revoke subscriptions and history on unload."""
        self._listeners.clear()
        self._seen.clear()
        self._seeded.clear()
        self._zones.clear()
