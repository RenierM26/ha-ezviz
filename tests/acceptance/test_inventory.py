"""Candidate SDK + real HA classes; synthetic account data and commands only."""

from copy import deepcopy
import json
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

from custom_components.ezviz_cloud import binary_sensor, camera, coordinator, light, sensor, switch
from custom_components.ezviz_cloud.availability import device_available
from custom_components.ezviz_cloud.const import DATA_COORDINATOR, DOMAIN
from custom_components.ezviz_cloud.inventory_binary_sensor import EzvizDetectorBinarySensor
from custom_components.ezviz_cloud.inventory_power import EzvizInventoryLight, EzvizInventoryPlug
from custom_components.ezviz_cloud.inventory_sensor import EzvizDetectorSensor
from homeassistant.config_entries import current_entry


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
        result = coordinator.EzvizDataUpdateCoordinator(hass, api=api, api_timeout=2)
    finally:
        current_entry.reset(token)
    result.async_set_updated_data(await result._async_update_data())
    return result


async def test_mixed_inventory_all_platforms_and_stable_camera_identity(hass, client, monkeypatch):
    api, _, calls = client
    data = await make(hass, api)
    assert set(data.data) == {"CAM001"}
    assert set(data.inventory_data) == {"CAM001", "DET001", "LIGHT1", "PLUG01"}
    hass.data[DOMAIN] = {"entry": {DATA_COORDINATOR: data}}
    entry = MagicMock(entry_id="entry", options={"cameras": {"CAM001": {"enc_key": "synthetic"}}})
    monkeypatch.setattr(camera, "get_ffmpeg_manager", lambda hass: MagicMock())
    monkeypatch.setattr(camera, "async_get_current_platform", MagicMock())
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
    hass.data[DOMAIN] = {"entry": {DATA_COORDINATOR: data}}
    monkeypatch.setattr(sensor, "migrate_unique_ids_with_coordinator", AsyncMock())
    entities = []
    entry = MagicMock(entry_id="entry")
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


async def test_legacy_sdk_fallback_and_no_extra_family_reads(hass):
    api = SimpleNamespace(load_cameras=MagicMock(return_value={"old": {"status": 1}}))
    data = await make(hass, api)
    assert data.data == {"old": {"status": 1}}
    assert data.inventory_snapshot is None and data.devices_of_kind("detector") == {}
    api.load_cameras.assert_called_once_with()
