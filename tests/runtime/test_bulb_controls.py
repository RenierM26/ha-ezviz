"""Metadata-gated bulb controls using recorded LB1 features and real SDK requests."""

import asyncio
from datetime import timedelta
import json
import logging
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
import pytest_asyncio
from requests import Response

import custom_components
from custom_components.ezviz_cloud import light
from custom_components.ezviz_cloud.const import DOMAIN
from homeassistant import loader
from homeassistant.components import light as ha_light
from homeassistant.components.light import ColorMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform, EntityPlatformModule

from .test_issue_173_bulb import SERIAL, bulb as recorded_bulb

bulb = recorded_bulb


def change(page, key, **updates):
    feature = json.loads(page["FEATURE"][SERIAL]["featureJson"])
    item = next(x for x in feature["featureItemDtos"] if x["itemKey"] == key)
    item.update(updates)
    page["FEATURE"][SERIAL]["featureJson"] = json.dumps(feature)


def drop(page, *keys):
    feature = json.loads(page["FEATURE"][SERIAL]["featureJson"])
    feature["featureItemDtos"] = [x for x in feature["featureItemDtos"] if x["itemKey"] not in keys]
    page["FEATURE"][SERIAL]["featureJson"] = json.dumps(feature)


def response(code=200):
    result = Response()
    result.status_code = code
    result._content = json.dumps({"meta": {"code": code}, "data": {}}).encode()
    return result


@pytest_asyncio.fixture
async def ready(hass, bulb, monkeypatch):
    page, api, fetch, _messages, entry, account = bulb
    sent = []
    def send(request, **_kwargs):
        sent.append(json.loads(request.body))
        return response()
    monkeypatch.setattr(api, "_send_prepared", send)
    await account.async_refresh()
    entities = []
    await light.async_setup_entry(hass, entry, entities.extend)
    try:
        yield SimpleNamespace(page=page, api=api, fetch=fetch, entry=entry, account=account, entity=entities[0], sent=sent)
    finally:
        await entry._async_process_on_unload(hass)
        await account.async_close()


@pytest_asyncio.fixture
async def registered(hass, ready):
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    await ha_light.async_setup(hass, {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="light",
        platform_name=DOMAIN, platform=cast(EntityPlatformModule, light),
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    platform.config_entry = ready.entry
    try:
        await platform.platform_data.async_load_translations()
        await platform.async_add_entities([ready.entity])
        yield ready
    finally:
        await platform.async_reset()


@pytest.mark.parametrize("target,expected", [
    ({"brightness": 128}, [("brightness", 50), ("light_switch", True)]),
    ({"brightness": 1}, [("brightness", 1), ("light_switch", True)]),
    ({"brightness": 255}, [("brightness", 100), ("light_switch", True)]),
    ({"color_temp_kelvin": 3333}, [("light_mode", "white"), ("color_temperature", 3330), ("light_switch", True)]),
    ({"color_temp_kelvin": 1000}, [("light_mode", "white"), ("color_temperature", 2700), ("light_switch", True)]),
    ({"color_temp_kelvin": 9000}, [("light_mode", "white"), ("color_temperature", 6500), ("light_switch", True)]),
    ({"rgb_color": (1, 2, 255)}, [("light_mode", "color"), ("color_rgb", "#0102FF"), ("light_switch", True)]),
    ({"rgb_color": (20, 30, 40), "brightness": 64}, [("light_mode", "color"), ("color_rgb", "#141E28"), ("brightness", 25), ("light_switch", True)]),
    ({"color_temp_kelvin": 3000, "brightness": 128}, [("light_mode", "white"), ("color_temperature", 3000), ("brightness", 50), ("light_switch", True)]),
    ({"brightness": 0, "rgb_color": (1, 2, 3)}, [("light_switch", False)]),
])
async def test_recorded_commands_are_quantized_serialized_and_not_optimistic(ready, target, expected):
    await ready.entity.async_turn_on(**target)
    assert [(x["itemKey"], x["value"]) for x in ready.sent] == expected
    assert all(x["productId"] == "issue173-product" for x in ready.sent)
    assert ready.entity.is_on is True and ready.entity.color_mode == ColorMode.COLOR_TEMP
    assert ready.entity.brightness == 255 and ready.entity.color_temp_kelvin == 6500
    assert ready.entity.rgb_color is None
    ready.fetch.assert_called_once()
    assert ready.account.settings._refresh_cancel is not None


async def test_rgb_and_white_state_follow_sdk_poll_not_accepted_targets(ready):
    entity = ready.entity
    assert entity.min_color_temp_kelvin == 2700 and entity.max_color_temp_kelvin == 6500
    await entity.async_turn_on(rgb_color=(16, 32, 48), brightness=128)
    assert entity.color_mode == ColorMode.COLOR_TEMP
    change(ready.page, "light_mode", dataValue="color")
    change(ready.page, "color_rgb", dataValue="#102030")
    change(ready.page, "brightness", dataValue=50)
    await ready.account.async_refresh()
    assert entity.color_mode == ColorMode.RGB and entity.rgb_color == (16, 32, 48)
    assert entity.brightness == 128 and entity.color_temp_kelvin is None
    await entity.async_turn_on(color_temp_kelvin=3000)
    assert entity.color_mode == ColorMode.RGB
    change(ready.page, "light_mode", dataValue="white")
    change(ready.page, "color_temperature", dataValue=3000)
    await ready.account.async_refresh()
    assert entity.color_mode == ColorMode.COLOR_TEMP and entity.color_temp_kelvin == 3000
    assert entity.rgb_color is None


@pytest.mark.parametrize("mode", ["scene", "music", "unknown", None])
async def test_unimplemented_modes_remain_unknown_not_fake_white_or_rgb(hass, registered, mode):
    ready = registered
    change(ready.page, "light_mode", dataValue=mode)
    await ready.account.async_refresh()
    assert ready.entity.color_mode == ColorMode.UNKNOWN
    assert ready.entity.color_temp_kelvin is None and ready.entity.rgb_color is None
    assert ready.entity.supported_color_modes == {ColorMode.COLOR_TEMP, ColorMode.RGB}
    assert hass.states.get(ready.entity.entity_id).attributes["color_mode"] == "unknown"


@pytest.mark.parametrize("invalid", [
    {"brightness": -1}, {"brightness": 256}, {"brightness": True}, {"brightness": float("nan")},
    {"color_temp_kelvin": 0}, {"color_temp_kelvin": True}, {"color_temp_kelvin": 3000.5},
    {"rgb_color": (1, 2)}, {"rgb_color": (1, 2, 256)}, {"rgb_color": (True, 2, 3)},
    {"rgb_color": "#FF0000"}, {"rgb_color": (1, 2, 3), "color_temp_kelvin": 3000},
])
async def test_invalid_targets_fail_before_any_property_is_written(ready, invalid):
    with pytest.raises(ServiceValidationError):
        await ready.entity.async_turn_on(**invalid)
    assert not ready.sent


@pytest.mark.parametrize("desc", ["bad json", "[]", '{"range_from":true,"range_to":100,"interval":1,"multiple":0,"unit":""}', '{"range_from":1,"range_to":100,"interval":0,"multiple":0,"unit":""}', '{"range_from":1,"range_to":255,"interval":1,"multiple":0,"unit":""}', '{"range_from":1,"range_to":100,"interval":1,"multiple":1,"unit":""}'])
async def test_unqualified_brightness_scale_preserves_existing_onoff_support(ready, desc):
    change(ready.page, "brightness", dataDesc=desc)
    await ready.account.async_refresh()
    assert ready.entity.supported_color_modes == {ColorMode.ONOFF}
    assert ready.entity.brightness is None
    with pytest.raises(ServiceValidationError):
        await ready.entity.async_turn_on(brightness=128)
    assert not ready.sent
    await ready.entity.async_turn_on()
    assert [(x["itemKey"], x["value"]) for x in ready.sent] == [("light_switch", True)]


async def test_missing_and_readonly_metadata_never_gains_unqualified_controls(ready):
    uid = ready.entity.unique_id
    drop(ready.page, "light_mode", "color_rgb", "color_temperature")
    await ready.account.async_refresh()
    assert ready.entity.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert ready.entity.color_mode == ColorMode.BRIGHTNESS
    change(ready.page, "brightness", transportType="r")
    await ready.account.async_refresh()
    assert ready.entity.supported_color_modes == {ColorMode.ONOFF}
    assert ready.entity.unique_id == uid
    with pytest.raises(ServiceValidationError):
        await ready.entity.async_turn_on(rgb_color=(1, 2, 3))
    assert not ready.sent


async def test_kelvin_unit_and_rgb_encoding_are_not_guessed(ready):
    change(ready.page, "color_temperature", dataDesc='{"range_from":153,"range_to":370,"interval":1,"multiple":0,"unit":"mired"}')
    change(ready.page, "color_rgb", dataValue="rgba(1,2,3,4)")
    await ready.account.async_refresh()
    assert ready.entity.supported_color_modes == {ColorMode.BRIGHTNESS}
    for target in [{"color_temp_kelvin": 3000}, {"rgb_color": (1, 2, 3)}]:
        with pytest.raises(ServiceValidationError):
            await ready.entity.async_turn_on(**target)
    assert not ready.sent


async def test_metadata_is_checked_after_waiting_for_the_account_write_lock(ready):
    async with ready.account.settings._write_lock:
        task = asyncio.create_task(ready.entity.async_turn_on(rgb_color=(1, 2, 3)))
        await asyncio.sleep(0)
        drop(ready.page, "color_rgb")
        await ready.account.async_refresh()
    with pytest.raises(ServiceValidationError):
        await task
    assert not ready.sent


async def test_quick_rgb_then_kelvin_keeps_sequences_whole_and_explicitly_selects_mode(ready, monkeypatch):
    entered, release = Event(), Event()
    def send(request, **_kwargs):
        ready.sent.append(json.loads(request.body))
        if len(ready.sent) == 1:
            entered.set()
            assert release.wait(5)
        return response()
    monkeypatch.setattr(ready.api, "_send_prepared", send)
    first = asyncio.create_task(ready.entity.async_turn_on(rgb_color=(1, 2, 3), brightness=128))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(ready.entity.async_turn_on(color_temp_kelvin=3000))
        await asyncio.sleep(0)
        assert len(ready.sent) == 1
        release.set()
        await first
        await second
        assert [(x["itemKey"], x["value"]) for x in ready.sent] == [("light_mode", "color"), ("color_rgb", "#010203"), ("brightness", 50), ("light_switch", True), ("light_mode", "white"), ("color_temperature", 3000), ("light_switch", True)]
    finally:
        release.set()
        await first
        if second:
            await second


async def test_partial_failure_stops_sequence_and_schedules_observation(ready, monkeypatch):
    def send(request, **_kwargs):
        ready.sent.append(json.loads(request.body))
        return response(403 if len(ready.sent) == 2 else 200)
    monkeypatch.setattr(ready.api, "_send_prepared", send)
    with pytest.raises(HomeAssistantError):
        await ready.entity.async_turn_on(rgb_color=(1, 2, 3), brightness=128)
    assert [x["itemKey"] for x in ready.sent] == ["light_mode", "color_rgb"]
    assert ready.entity.color_mode == ColorMode.COLOR_TEMP
    assert ready.account.settings._refresh_cancel is not None


@pytest.mark.parametrize("failed", [False, True])
async def test_canceled_waiter_still_owns_sequence_and_refresh(ready, monkeypatch, failed):
    entered, release = Event(), Event()
    def send(request, **_kwargs):
        ready.sent.append(json.loads(request.body))
        if len(ready.sent) == 2:
            entered.set()
            assert release.wait(5)
            if failed:
                return response(403)
        return response()
    monkeypatch.setattr(ready.api, "_send_prepared", send)
    task = asyncio.create_task(ready.entity.async_turn_on(rgb_color=(1, 2, 3)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        async with asyncio.timeout(2):
            while ready.account._action_futures:
                await asyncio.sleep(0.005)
        assert [x["itemKey"] for x in ready.sent] == (["light_mode", "color_rgb"] if failed else ["light_mode", "color_rgb", "light_switch"])
        assert ready.account.settings._refresh_cancel is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("first_writable", [False, True])
async def test_duplicate_metadata_matches_the_sdk_first_item_not_the_last(ready, first_writable):
    feature = json.loads(ready.page["FEATURE"][SERIAL]["featureJson"])
    item = next(x for x in feature["featureItemDtos"] if x["itemKey"] == "brightness")
    item["transportType"] = "rw" if first_writable else "r"
    duplicate = {**item, "transportType": "r" if first_writable else "rw", "dataValue": 50}
    feature["featureItemDtos"].append(duplicate)
    ready.page["FEATURE"][SERIAL]["featureJson"] = json.dumps(feature)
    await ready.account.async_refresh()
    assert ready.entity.brightness == (255 if first_writable else None)
    assert ready.entity.supported_color_modes == ({ColorMode.COLOR_TEMP, ColorMode.RGB} if first_writable else {ColorMode.ONOFF})


@pytest.mark.parametrize("target,mode,value_key,value", [
    ({"rgb_color": [1, 2, 3], "brightness": 128}, "color", "color_rgb", "#010203"),
    ({"color_temp_kelvin": 3333, "brightness": 128}, "white", "color_temperature", 3330),
])
async def test_native_ha_colour_services_and_observed_ui_state(hass, registered, target, mode, value_key, value):
    ready = registered
    await hass.services.async_call("light", "turn_on", {"entity_id": ready.entity.entity_id, **target}, blocking=True)
    assert [(x["itemKey"], x["value"]) for x in ready.sent] == [("light_mode", mode), (value_key, value), ("brightness", 50), ("light_switch", True)]
    state = hass.states.get(ready.entity.entity_id)
    assert state.attributes["color_mode"] == "color_temp" and state.attributes["color_temp_kelvin"] == 6500
    change(ready.page, "light_mode", dataValue=mode)
    change(ready.page, value_key, dataValue=value)
    change(ready.page, "brightness", dataValue=50)
    await ready.account.async_refresh()
    state = hass.states.get(ready.entity.entity_id)
    assert state.attributes["brightness"] == 128
    if mode == "color":
        assert state.attributes["color_mode"] == "rgb" and state.attributes["rgb_color"] == (1, 2, 3)
    else:
        assert state.attributes["color_mode"] == "color_temp" and state.attributes["color_temp_kelvin"] == 3330
