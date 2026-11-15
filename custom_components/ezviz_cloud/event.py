"""Doorbell and access events, independent of alarm pictures or motion state."""

from typing import override

from pyezvizapi.devices.intercom import device_category
from pyezvizapi.devices.intercom_events import IntercomEvent
from pyezvizapi.devices.snapshot import DeviceState

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizInventoryEntity
from .inventory import async_add_inventory_entities
from .runtime import EzvizConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Discover doorbell event entities from the accepted SDK inventory."""
    async_add_inventory_entities(
        entry.runtime_data.coordinator,
        entry,
        async_add_entities,
        "camera",
        intercom_events,
    )


def intercom_events(
    coordinator: EzvizDataUpdateCoordinator, serial: str, state: DeviceState
) -> list[EzvizIntercomEvent]:
    """Ordinary cameras and indoor monitors do not get guessed doorbell events."""
    if device_category(state.values) != "BDoorBell":
        return []
    return [
        EzvizIntercomEvent(coordinator, serial, kind)
        for kind in ("ring", "lock_unlocked")
    ]


class EzvizIntercomEvent(EzvizInventoryEntity, EventEntity):
    """Report an occurrence, never a persistent door/lock contact state."""

    def __init__(
        self, coordinator: EzvizDataUpdateCoordinator, serial: str, kind: str
    ) -> None:
        """Associate one event type with the existing device identity."""
        super().__init__(coordinator, serial)
        self._kind = kind
        self._attr_unique_id = f"{serial}_intercom_{kind}"
        self._attr_translation_key = f"intercom_{kind}"
        self._attr_event_types = [kind]
        if kind == "ring":
            self._attr_device_class = EventDeviceClass.DOORBELL

    @override
    async def async_added_to_hass(self) -> None:
        """Remove the subscription on unload and before a registry re-add."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.intercom_events.subscribe(self.serial, self._event)
        )

    @callback
    def _event(self, event: IntercomEvent) -> None:
        if event.event_type != self._kind:
            return
        self._trigger_event(
            event.event_type,
            {
                "event_id": event.event_id,
                "occurred_at": event.occurred_at.isoformat()
                if event.occurred_at
                else None,
                "channel": event.channel,
                "method": event.method,
            },
        )
        self.async_write_ha_state()
