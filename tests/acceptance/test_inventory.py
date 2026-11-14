"""Candidate SDK + real HA classes; synthetic account data and commands only."""

import asyncio
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi import (
    EzvizClient,
)
from pyezvizapi.constants import DeviceSwitchType
from pyezvizapi.devices import factory
from pyezvizapi.models import EzvizDeviceRecord
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_cloud import (
    binary_sensor,
    button,
    camera,
    coordinator,
    light,
    runtime,
    sensor,
    siren,
    switch,
    update,
)
from custom_components.ezviz_cloud.availability import device_available
from custom_components.ezviz_cloud.binary_sensor import EzvizDetectorBinarySensor
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.light import EzvizInventoryLight
from custom_components.ezviz_cloud.sensor import EzvizDetectorSensor
from custom_components.ezviz_cloud.switch import EzvizInventoryPlug
from homeassistant.config_entries import current_entry
from homeassistant.exceptions import HomeAssistantError


def info(serial, category, **extra):
    return {
        "deviceInfos": {
            "deviceSerial": serial,
            "deviceCategory": category,
            "name": f"Synthetic {serial}",
            "deviceSubCategory": category,
            "version": "V1",
            "status": 1,
            "supportExt": {},
            **extra,
        },
        "WIFI": {},
        "STATUS": {},
        "SWITCH": {},
    }


@pytest.fixture
def client(monkeypatch):
    api = EzvizClient(
        token={
            "session_id": "synthetic",
            "api_url": "api.example.invalid",
            "username": "synthetic-user",
        }
    )
    records = {
        s: info(s, category)
        for s, category in (
            ("CAM001", "IPC"),
            ("DET001", "Detector"),
            ("LIGHT1", "lighting"),
            ("PLUG01", "Socket"),
        )
    }
    records["DET001"]["FEATURE_INFO"] = {
        "0": {"global": {"DoorMagnetic": {"DoorStatus": 0}, "BatteryInfo": {"SurplusPower": 0}}}
    }
    records["LIGHT1"]["FEATURE"] = {
        "featureJson": json.dumps(
            {
                "productId": "synthetic-product",
                "featureItemDtos": [{"itemKey": "light_switch", "dataValue": True}],
            }
        )
    }
    records["PLUG01"]["SWITCH"] = [{"type": DeviceSwitchType.PLUG.value, "enable": 0}]
    calls = []

    def fetch(_):
        calls.append(1)
        return {s: EzvizDeviceRecord.from_api(s, deepcopy(raw)) for s, raw in records.items()}

    monkeypatch.setattr(api, "get_device_records", fetch)
    monkeypatch.setattr(api, "get_device_messages_list", lambda **kwargs: {"messages": []})
    yield api, records, calls
    api.close_session()


async def make(hass, api):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    token = current_entry.set(entry)
    try:
        result = coordinator.EzvizDataUpdateCoordinator(
            hass, config_entry=entry, api=api, api_timeout=2
        )
    finally:
        current_entry.reset(token)
    result.async_set_updated_data(await result._async_update_data())
    return result


async def test_mixed_inventory_all_platforms_and_stable_camera_identity(hass, client, monkeypatch):
    api, _, calls = client
    data = await make(hass, api)
    assert set(data.data) == {"CAM001"}
    assert set(data.inventory_data) == {"CAM001", "DET001", "LIGHT1", "PLUG01"}
    entry = MagicMock(
        runtime_data=SimpleNamespace(coordinator=data),
        entry_id="entry",
        options={"cameras": {"CAM001": {"enc_key": "synthetic"}}},
    )
    monkeypatch.setattr(camera, "get_ffmpeg_manager", lambda hass: MagicMock())
    for mod in (sensor, binary_sensor, switch):
        monkeypatch.setattr(mod, "migrate_unique_ids_with_coordinator", AsyncMock())
    entities = []
    for mod in (camera, sensor, binary_sensor, light, switch):
        await mod.async_setup_entry(hass, entry, entities.extend)
    assert len(calls) == 1  # No platform performs another inventory acquisition.
    cameras = [e for e in entities if isinstance(e, camera.EzvizCamera)]
    assert len(cameras) == 1 and cameras[0].unique_id == "CAM001"
    detectors = [e for e in entities if isinstance(e, EzvizDetectorSensor)]
    assert {e.unique_id for e in detectors} == {"DET001_battery_level", "DET001_door_status"}
    assert next(e for e in detectors if e.unique_id.endswith("battery_level")).native_value == 0
    binary = [e for e in entities if isinstance(e, EzvizDetectorBinarySensor)]
    assert len(binary) == 1 and binary[0].is_on is None
    assert not binary[0].entity_registry_enabled_default
    assert [e.unique_id for e in entities if isinstance(e, EzvizInventoryLight)] == ["LIGHT1_power"]
    assert [e.unique_id for e in entities if isinstance(e, EzvizInventoryPlug)] == ["PLUG01_power"]
    assert all(e.available for e in entities)
    for e in entities:
        assert e.device_info["identifiers"] == {(DOMAIN, e._serial)}


async def test_poll_health_missing_device_recovery_and_additions(hass, client, monkeypatch):
    api, records, _ = client
    data = await make(hass, api)
    monkeypatch.setattr(sensor, "migrate_unique_ids_with_coordinator", AsyncMock())
    entities = []
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=data), entry_id="entry")
    await sensor.async_setup_entry(hass, entry, entities.extend)
    battery = next(e for e in entities if e.unique_id == "DET001_battery_level")
    first = data.inventory_snapshot
    removed = records.pop("DET001")
    data.async_set_updated_data(await data._async_update_data())
    assert not battery.available and battery.native_value == 0
    assert not data.inventory_snapshot.health["DET001"].present
    records["DET001"] = removed
    records["NEWDET"] = deepcopy(removed)
    records["NEWDET"]["deviceInfos"]["deviceSerial"] = "NEWDET"
    data.async_set_updated_data(await data._async_update_data())
    assert battery.available
    assert len([e for e in entities if e.unique_id == "DET001_battery_level"]) == 1
    assert any(e.unique_id == "NEWDET_battery_level" for e in entities)
    data.last_update_success = False
    assert not battery.available
    data.last_update_success = True
    assert first.devices["DET001"].battery_level == 0


async def test_per_device_render_failure_is_unavailable_not_global(hass, client, monkeypatch):
    api, _, _ = client
    data = await make(hass, api)
    original = factory.camera_status
    monkeypatch.setattr(
        factory, "camera_status", MagicMock(side_effect=ValueError("PRIVATE_CANARY"))
    )
    data.async_set_updated_data(await data._async_update_data())
    assert not device_available(data, "CAM001")
    assert device_available(data, "DET001")
    assert data.inventory_snapshot.health["CAM001"].error_type == "ValueError"
    monkeypatch.setattr(factory, "camera_status", original)
    data.async_set_updated_data(await data._async_update_data())
    assert device_available(data, "CAM001")


async def test_power_commands_use_cached_metadata_without_optimistic_state(
    hass, client, monkeypatch
):
    api, _, calls = client
    data = await make(hass, api)
    data.async_request_refresh = AsyncMock()
    bulb, plug = EzvizInventoryLight(data, "LIGHT1"), EzvizInventoryPlug(data, "PLUG01")
    bulb.hass = plug.hass = hass
    set_feature, set_switch = MagicMock(return_value=True), MagicMock(return_value=True)
    monkeypatch.setattr(api, "set_device_feature_by_key", set_feature)
    monkeypatch.setattr(api, "set_switch", set_switch)
    assert bulb.is_on is True and plug.is_on is False
    await bulb.async_turn_off()
    await plug.async_turn_on()
    set_feature.assert_called_once_with("LIGHT1", "synthetic-product", False, "light_switch")
    set_switch.assert_called_once_with("PLUG01", DeviceSwitchType.PLUG.value, 1)
    assert len(calls) == 1 and data.async_request_refresh.await_count == 2
    assert bulb.is_on is True and plug.is_on is False


async def test_push_copy_does_not_make_stale_poll_available_or_change_detectors(hass, client):
    api, _, _ = client
    data = await make(hass, api)
    first = deepcopy(data.data)
    event = {"ext": {"image": "synthetic", "alert_type_code": 1}}
    detector = data.inventory_snapshot.devices["DET001"]
    data.merge_mqtt_update("DET001", event)
    assert data.inventory_snapshot.devices["DET001"] is detector
    data.merge_mqtt_update("CAM001", event)
    assert first["CAM001"]["last_alarm_pic"] != "synthetic"
    assert data.data["CAM001"]["last_alarm_pic"] == "synthetic"
    data.last_update_success = False
    data.merge_mqtt_update("CAM001", event)
    # Push is not a successful authoritative whole-inventory poll.
    assert not data.last_update_success


async def test_plug_status_uses_device_channel_not_last_outlet(hass, client):
    api, records, _ = client
    rows = [
        {"type": DeviceSwitchType.PLUG.value, "channelNo": 0, "enable": False},
        {"type": DeviceSwitchType.PLUG.value, "channelNo": 1, "enable": True},
    ]
    records["PLUG01"]["SWITCH"] = rows
    data = await make(hass, api)
    plug = EzvizInventoryPlug(data, "PLUG01")
    assert plug.is_on is False and plug.available
    records["PLUG01"]["SWITCH"] = list(reversed(rows))
    data.async_set_updated_data(await data._async_update_data())
    assert plug.is_on is False and plug.unique_id == "PLUG01_power"


@pytest.mark.parametrize(
    "code,phase,active,needed",
    [
        (1, "rebooting", True, 1),
        (14, "waiting", True, 2),
        (15, "downloading", True, 1),
        (2, "current", False, 0),
        (999, None, False, 0),
    ],
)
async def test_sdk_firmware_status_reaches_real_ha_update_entity(
    hass, client, code, phase, active, needed
):
    api, records, _ = client
    for raw in records.values():
        raw["STATUS"].update(upgradeStatus=code, upgradeProcess=25)
        raw["UPGRADE"] = {"isNeedUpgrade": needed, "upgradePackageInfo": {"version": "V2"}}
    data = await make(hass, api)
    for state in data.inventory_data.values():
        assert state["upgrade_phase"] == phase
        assert state["upgrade_in_progress"] is active
        assert state["upgrade_available"] is (needed > 0)
    entity = update.EzvizUpdateEntity(data, "CAM001", update.UPDATE_ENTITY_DESCRIPTIONS[0])
    assert entity.unique_id == "CAM001_version"
    assert entity.in_progress is active
    assert entity.update_percentage == (25 if active else None)
    assert entity.latest_version == ("V2" if needed else "V1")
    assert entity.extra_state_attributes == {"upgrade_status": code, "upgrade_phase": phase}


def make_owned_camera_action(hass, api, data, monkeypatch, command, action):
    if action == "firmware":
        monkeypatch.setattr(api, "upgrade_device", command)
        entity = update.EzvizUpdateEntity(data, "CAM001", update.UPDATE_ENTITY_DESCRIPTIONS[0])
        return entity, entity.async_install(None, False)
    elif action == "switch":
        monkeypatch.setattr(api, "set_camera_defence", command)
        desc = switch.EzvizSwitchEntityDescription(
            key="synthetic",
            value_fn=lambda d: False,
            method=lambda sdk, serial, value: sdk.set_camera_defence(serial, value),
        )
        entity = switch.EzvizSwitch(data, "CAM001", desc)
        return entity, entity.async_turn_on()
    elif action == "motion":
        monkeypatch.setattr(api, "set_camera_defence", command)
        monkeypatch.setattr(camera, "get_ffmpeg_manager", lambda h: MagicMock())
        entity = camera.EzvizCamera(
            hass=hass,
            coordinator=data,
            serial="CAM001",
            camera_username="admin",
            camera_password="synthetic",
            rtsp_path="/synthetic",
        )
        return entity, entity.async_enable_motion_detection()
    else:
        monkeypatch.setattr(api, "reboot_camera", command)
        desc = button.EzvizButtonEntityDescription(
            key="synthetic", method=lambda sdk, serial: sdk.reboot_camera(serial)
        )
        entity = button.EzvizButtonEntity(data, "CAM001", desc)
        return entity, entity.async_press()


@pytest.mark.parametrize("action", ["firmware", "switch", "motion", "button"])
async def test_cancelled_camera_command_is_drained_before_session_close(
    hass, client, monkeypatch, action
):
    """Use real entity actions and a blocked worker, not a fake owned-task counter."""

    api, _, _ = client
    data = await make(hass, api)
    owner = runtime.RuntimeCleanup(hass, data)
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.01)
    entered, release = Event(), Event()
    order = []

    def command(*args):
        entered.set()
        assert release.wait(5)
        order.append("command")
        return True

    original_close = api.close_session

    def close():
        order.append("close")
        original_close()

    monkeypatch.setattr(api, "close_session", close)
    entity, call = make_owned_camera_action(hass, api, data, monkeypatch, command, action)
    entity.hass = hass
    task = asyncio.create_task(call)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.async_stop()
        assert not order and not owner.task.done()
        with pytest.raises(HomeAssistantError, match="shutting down"):
            await data.async_execute(command)
    finally:
        release.set()
    await owner.task
    assert order == ["command", "close"]
    assert not data._action_futures
    await owner.async_stop()
    assert order == ["command", "close"]


async def test_camera_siren_waits_for_owned_command_before_reporting_on(hass, client, monkeypatch):

    api, _, _ = client
    data = await make(hass, api)
    entered, release = Event(), Event()

    def command(*args):
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(api, "sound_alarm", command)
    entity = siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", MagicMock())
    task = asyncio.create_task(entity.async_turn_on())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
    finally:
        release.set()
    await task
    assert entity.is_on
    if entity._delay_listener is not None:
        entity._delay_listener()
        entity._delay_listener = None
