"""Replay the recorded LB1 through published SDK parsing and real HA adapters."""

from copy import deepcopy
from datetime import timedelta
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from pyezvizapi import EzvizClient
from pyezvizapi.devices.snapshot import LightState
import pytest
from requests import Response

import custom_components
from custom_components.ezviz_cloud import light
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from homeassistant import loader
from homeassistant.components import light as ha_light
from homeassistant.components.light import ColorMode
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform, EntityPlatformModule

from .test_hardening import entry_for

SERIAL = "ISSUE173LB1"
FIXTURE = Path(__file__).parents[1] / "fixtures/issue_173_lb1_pagelist.json"


def set_power(page, value):
    feature = json.loads(page["FEATURE"][SERIAL]["featureJson"])
    next(x for x in feature["featureItemDtos"] if x["itemKey"] == "light_switch")["dataValue"] = value
    page["FEATURE"][SERIAL]["featureJson"] = json.dumps(feature)


@pytest.fixture
def bulb(hass, monkeypatch):
    page = json.loads(FIXTURE.read_text())
    api = EzvizClient(token={"session_id": "synthetic", "api_url": "api.example.invalid", "username": "fixture-user"})
    fetch = MagicMock(side_effect=lambda: deepcopy(page))
    monkeypatch.setattr(api, "_get_page_list", fetch)
    messages = MagicMock(side_effect=AssertionError("Bulb must not fetch camera alarms"))
    monkeypatch.setattr(api, "get_device_messages_list", messages)
    entry = entry_for(hass)
    account = EzvizDataUpdateCoordinator(hass, config_entry=entry, api=api, api_timeout=5)
    entry.runtime_data = SimpleNamespace(coordinator=account)
    return page, api, fetch, messages, entry, account


@pytest.mark.parametrize("power", [False, True])
@pytest.mark.parametrize("online", [False, True])
async def test_recorded_lb1_discovery_and_observed_features(hass, bulb, power, online):
    page, _api, fetch, messages, entry, account = bulb
    set_power(page, power)
    page["deviceInfos"][0]["status"] = 1 if online else 2
    try:
        await account.async_refresh()
        assert account.last_update_success and account.data == {}  # No camera projection.
        state = account.device_state(SERIAL)
        assert isinstance(state, LightState) and state.is_on is power
        assert state.online is online
        assert state.values["brightness"] == 100 and state.values["color_temperature"] == 6500
        items = {x["itemKey"]: x.get("dataValue") for x in state.values["featureItems"]}
        assert items["light_mode"] == "white" and items["color_rgb"] == "#FF8F04"
        assert state.capabilities.has("brightness") is True
        added = []
        await light.async_setup_entry(hass, entry, added.extend)
        assert len(added) == 1 and isinstance(added[0], light.EzvizInventoryLight)
        entity = added[0]
        assert entity.unique_id == f"{SERIAL}_power" and entity.is_on is power
        assert entity.available is online and entity.device_info["model"] == "LB1"
        assert entity.supported_color_modes == {ColorMode.ONOFF}
        assert entity.brightness is None and entity.color_temp_kelvin is None and entity.rgb_color is None
        fetch.assert_called_once()
        messages.assert_not_called()
    finally:
        await entry._async_process_on_unload(hass)
        await account.async_close()


@pytest.mark.parametrize("on", [False, True])
async def test_recorded_lb1_encodes_real_sdk_power_command_without_optimism(hass, bulb, monkeypatch, on):
    page, api, fetch, messages, entry, account = bulb
    set_power(page, not on)
    reply = Response()
    reply.status_code = 200
    reply._content = b'{"meta":{"code":200},"data":{}}'
    send = MagicMock(return_value=reply)
    monkeypatch.setattr(api, "_send_prepared", send)
    try:
        await account.async_refresh()
        added = []
        await light.async_setup_entry(hass, entry, added.extend)
        entity = added[0]
        if on:
            await entity.async_turn_on()
        else:
            await entity.async_turn_off()
        send.assert_called_once()
        request = send.call_args.args[0]
        assert request.method == "PUT" and request.url.endswith(f"/{SERIAL}/0")
        assert json.loads(request.body) == {"itemKey": "light_switch", "productId": "issue173-product", "value": on}
        assert entity.is_on is not on  # Accepted command is not observed state.
        fetch.assert_called_once()
        set_power(page, on)
        await account.async_refresh()
        assert entity.is_on is on and entity.available
        assert fetch.call_count == 2
        messages.assert_not_called()
    finally:
        await entry._async_process_on_unload(hass)
        await account.async_close()


async def test_recorded_offline_bulb_refuses_control_without_a_request(hass, bulb, monkeypatch):
    page, api, _, _, entry, account = bulb
    page["deviceInfos"][0]["status"] = 2
    send = MagicMock(side_effect=AssertionError("Offline control must not send"))
    monkeypatch.setattr(api, "_send_prepared", send)
    try:
        await account.async_refresh()
        added = []
        await light.async_setup_entry(hass, entry, added.extend)
        with pytest.raises(HomeAssistantError):
            await added[0].async_turn_on()
        send.assert_not_called()
    finally:
        await entry._async_process_on_unload(hass)
        await account.async_close()


async def test_recorded_lb1_registered_light_service_and_observed_state(hass, bulb, monkeypatch):
    page, api, _, _, entry, account = bulb
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    await ha_light.async_setup(hass, {})
    reply = Response()
    reply.status_code = 200
    reply._content = b'{"meta":{"code":200},"data":{}}'
    send = MagicMock(return_value=reply)
    monkeypatch.setattr(api, "_send_prepared", send)
    await account.async_refresh()
    entities = []
    await light.async_setup_entry(hass, entry, entities.extend)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="light",
        platform_name=DOMAIN, platform=cast(EntityPlatformModule, light),
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.platform_data.async_load_translations()
        await platform.async_add_entities(entities)
        entity = entities[0]
        state = hass.states.get(entity.entity_id)
        assert state.state == "on" and state.attributes["supported_color_modes"] == ["onoff"]
        assert "brightness" not in state.attributes and "rgb_color" not in state.attributes
        await hass.services.async_call("light", "turn_off", {"entity_id": entity.entity_id}, blocking=True)
        assert json.loads(send.call_args.args[0].body)["value"] is False
        assert hass.states.get(entity.entity_id).state == "on"
        set_power(page, False)
        await account.async_refresh()
        assert hass.states.get(entity.entity_id).state == "off"
    finally:
        await platform.async_reset()
        await entry._async_process_on_unload(hass)
        await account.async_close()
