"""Exercise Home Assistant's device deletion against a synthetic EZVIZ account."""

from itertools import count
from unittest.mock import Mock

from pyezvizapi import EzvizClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_cloud.const import ATTR_TYPE_CLOUD, DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from homeassistant.config_entries import (
    ConfigEntryState,
    current_entry,
    support_remove_from_device,
)
from homeassistant.const import CONF_TYPE
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component


async def test_delete_device_through_ha(hass, hass_ws_client, monkeypatch):
    """Protect present cameras and the account alarm, and remove stale entities."""
    api = EzvizClient(
        token={
            "session_id": "synthetic",
            "api_url": "api.example.invalid",
            "username": "synthetic-user",
        }
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TYPE: ATTR_TYPE_CLOUD}, unique_id="account")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.supports_remove_device = await support_remove_from_device(hass, DOMAIN)
    assert entry.supports_remove_device
    context = current_entry.set(entry)
    try:
        coordinator = EzvizDataUpdateCoordinator(hass, config_entry=entry, api=api, api_timeout=2)
    finally:
        current_entry.reset(context)
    entry.runtime_data = RuntimeCleanup(hass, coordinator)

    # The rendered poll is cached and still shows the previously known camera.
    # A different account device that never rendered may still be on the list.
    coordinator.data = {"STALE": {}}
    request = Mock()
    monkeypatch.setattr(api, "_request_json", request)
    assert await async_setup_component(hass, "config", {})
    ws = await hass_ws_client(hass)
    registry = dr.async_get(hass)
    entities = er.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "STALE")}
    )
    present = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "PRESENT")}
    )
    alarm = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "account")}
    )
    stale_entity = entities.async_get_or_create(
        "camera", DOMAIN, "STALE", config_entry=entry, device_id=stale.id
    )

    message_ids = count(1)

    async def remove(device):
        await ws.send_json(
            {
                "id": next(message_ids),
                "type": "config/device_registry/remove",
                "device_id": device.id,
            }
        )
        return await ws.receive_json()

    try:
        assert not (await remove(alarm))["success"]
        request.assert_not_called()

        # Neither a failed request nor a missing inventory section is an empty account.
        request.side_effect = [OSError("EZVIZ unavailable"), {"meta": {"code": 200}}]
        for _ in range(2):
            assert not (await remove(stale))["success"]
            assert registry.async_get(stale.id) is not None
            assert entities.async_get(stale_entity.entity_id) is not None

        # Preserve an offline camera on the second page, even while absent from
        # the coordinator's rendered data.
        first_page = {
            "meta": {"code": 200},
            "page": {"hasNext": True},
            "deviceInfos": [{"deviceSerial": "OTHER"}],
        }
        last_page = {
            "meta": {"code": 200},
            "page": {"hasNext": False},
            "deviceInfos": [{"deviceSerial": "PRESENT", "status": 0}],
        }
        request.side_effect = [first_page, last_page, first_page, last_page]
        assert not (await remove(present))["success"]
        assert registry.async_get(present.id) is not None

        assert (await remove(stale))["success"]
        await hass.async_block_till_done()
        assert registry.async_get(stale.id) is None
        assert entities.async_get(stale_entity.entity_id) is None
        assert registry.async_get(present.id) is not None
        assert registry.async_get(alarm.id) is not None
    finally:
        await coordinator.async_close()
