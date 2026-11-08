"""Shared discovery and command glue for non-camera coordinator views."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyezvizapi.constants import DeviceSwitchType
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.exceptions import HomeAssistantError


def async_add_inventory_entities(
    coordinator: Any, entry: Any, add: Callable, kind: str, factory: Callable
) -> None:
    """Discover additions/observed properties without another network refresh."""
    seen: set[str] = set()

    def discover() -> None:
        entities = []
        for serial, state in coordinator.devices_of_kind(kind).items():
            for entity in factory(coordinator, serial, state):
                if entity.unique_id not in seen:
                    seen.add(entity.unique_id)
                    entities.append(entity)
        if entities:
            add(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


def power_command(client: Any, serial: str, state: Any, on: bool) -> Any:
    """Use the SDK's existing on/off operations, with cached command metadata."""
    if state.kind == "plug":
        return client.set_switch(serial, DeviceSwitchType.PLUG.value, int(on))
    values = state.values
    item = next(
        (item for item in values.get("featureItems", ()) if item.get("itemKey") == "light_switch"),
        None,
    )
    if item is None or not values.get("productId"):
        raise PyEzvizError("Light has no observed on/off command metadata")
    return client.set_device_feature_by_key(serial, values["productId"], on, item["itemKey"])


async def async_set_power(entity: Any, on: bool) -> None:
    """No optimistic update; a successful command is followed by shared polling."""
    if not entity.available or entity.device_state is None:
        raise HomeAssistantError("EZVIZ device is unavailable")
    try:
        result = await entity.coordinator.async_execute(
            power_command, entity.coordinator.ezviz_client, entity._serial, entity.device_state, on
        )
        if result is False:
            raise HomeAssistantError("EZVIZ did not accept the power command")
    except (HTTPError, PyEzvizError, OSError, TimeoutError) as error:
        raise HomeAssistantError(f"EZVIZ power command failed ({type(error).__name__})") from error
    await entity.coordinator.async_request_refresh()
