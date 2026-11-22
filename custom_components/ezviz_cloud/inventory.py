"""Shared discovery and command glue for non-camera coordinator views."""

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceSwitchType
from pyezvizapi.devices.snapshot import DeviceKind, DeviceState
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizInventoryEntity
from .errors import request_error

if TYPE_CHECKING:
    from .runtime import EzvizConfigEntry


def async_add_inventory_entities(
    coordinator: EzvizDataUpdateCoordinator,
    entry: EzvizConfigEntry,
    add: AddConfigEntryEntitiesCallback,
    kind: DeviceKind,
    factory: Callable[
        [EzvizDataUpdateCoordinator, str, DeviceState], Iterable[EzvizInventoryEntity]
    ],
) -> None:
    """Discover additions/observed properties without another network refresh."""
    _async_discover_entities(
        coordinator,
        entry,
        add,
        lambda: (
            entity
            for serial, state in coordinator.devices_of_kind(kind).items()
            for entity in factory(coordinator, serial, state)
        ),
    )


def async_add_camera_inventory_entities[EntityT: Entity](
    coordinator: EzvizDataUpdateCoordinator,
    entry: EzvizConfigEntry,
    add: AddConfigEntryEntitiesCallback,
    factory: Callable[[str, dict[str, Any], EntityT | None], EntityT | None],
) -> None:
    """Discover or update one entity per camera on each accepted poll."""
    known: dict[str, EntityT] = {}

    @callback
    def discover() -> Iterable[Entity]:
        for serial, data in (coordinator.data or {}).items():
            existing = known.get(serial)
            entity = factory(serial, data, existing)
            if existing is None and entity is not None:
                known[serial] = entity
                yield entity

    _async_discover_entities(coordinator, entry, add, discover)


def _async_discover_entities(
    coordinator: EzvizDataUpdateCoordinator,
    entry: EzvizConfigEntry,
    add: AddConfigEntryEntitiesCallback,
    factory: Callable[[], Iterable[Entity]],
) -> None:
    seen: set[str] = set()
    accepted_snapshot: object = object()

    @callback
    def discover() -> None:
        nonlocal accepted_snapshot
        if coordinator.inventory_snapshot is accepted_snapshot:
            return
        accepted_snapshot = coordinator.inventory_snapshot
        entities = []
        for entity in factory():
            if (unique_id := entity.unique_id) is not None and unique_id not in seen:
                seen.add(unique_id)
                entities.append(entity)
        if entities:
            add(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


def power_command(
    client: EzvizClient, serial: str, state: DeviceState, on: bool
) -> Any:
    """Use the SDK's existing on/off operations, with cached command metadata."""
    if state.kind == "plug":
        return client.set_switch(serial, DeviceSwitchType.PLUG.value, int(on))
    values = state.values
    item = next(
        (
            item
            for item in values.get("featureItems", ())
            if item.get("itemKey") == "light_switch"
        ),
        None,
    )
    if item is None or not values.get("productId"):
        raise PyEzvizError("Light has no observed on/off command metadata")
    return client.set_device_feature_by_key(
        serial, values["productId"], on, item["itemKey"]
    )


async def async_set_power(entity: EzvizInventoryEntity, on: bool) -> None:
    """Refresh observed power settings through the shared account owner."""
    state = entity.device_state
    if not entity.available or state is None:
        raise HomeAssistantError("EZVIZ device is unavailable")
    try:
        await entity.async_write_setting(
            power_command,
            entity.coordinator.ezviz_client,
            entity.serial,
            state,
            on,
        )
    except (HTTPError, PyEzvizError, OSError, TimeoutError) as error:
        raise request_error("Set device power", error) from error
