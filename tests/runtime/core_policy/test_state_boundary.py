"""Real published SDK producers feeding the native HA state boundary."""

from collections.abc import Generator
from copy import deepcopy
import json
from typing import Any
from unittest.mock import MagicMock

from custom_components.ezviz_cloud.availability import device_available
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.entity import EzvizEntity, EzvizInventoryEntity
from custom_components.ezviz_cloud.runtime import EzvizConfigEntry
from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceSwitchType
from pyezvizapi.devices import factory
from pyezvizapi.devices.snapshot import (
    CameraState,
    DetectorState,
    LightState,
    PlugState,
)
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.models import EzvizDeviceRecord
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from tests.runtime.test_hardening import entry_for

type Records = dict[str, dict[str, Any]]
type Producer = tuple[EzvizClient, Records, list[int]]


@pytest.fixture
def producer(monkeypatch: pytest.MonkeyPatch) -> Generator[Producer]:
    """Mock acquisition, not SDK rendering, typing, retention or health policy."""
    api = EzvizClient(token={"session_id": "synthetic", "api_url": "private.invalid"})
    records = {
        serial: {
            "deviceInfos": {
                "deviceSerial": serial,
                "deviceCategory": category,
                "deviceSubCategory": category,
                "name": "Synthetic",
                "version": "1",
                "status": 1,
                "supportExt": {},
            },
            "WIFI": {},
            "STATUS": {},
            "SWITCH": {},
        }
        for serial, category in (
            ("CAM001", "IPC"),
            ("DET001", "Detector"),
            ("LIGHT1", "lighting"),
            ("PLUG01", "Socket"),
        )
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
    calls: list[int] = []

    def fetch(_categories: object) -> dict[str, EzvizDeviceRecord]:
        calls.append(1)
        return {
            serial: EzvizDeviceRecord.from_api(serial, deepcopy(raw))
            for serial, raw in records.items()
        }

    monkeypatch.setattr(api, "get_device_records", fetch)
    monkeypatch.setattr(
        api, "get_device_messages_list", lambda **_kwargs: {"messages": []}
    )
    yield api, records, calls
    api.close_session()


def coordinator_for(
    hass: HomeAssistant, api: EzvizClient
) -> EzvizDataUpdateCoordinator:
    """Build the real HA coordinator without setting up platforms or network IO."""
    entry: EzvizConfigEntry = entry_for(hass)
    return EzvizDataUpdateCoordinator(hass, config_entry=entry, api=api, api_timeout=2)


@pytest.mark.parametrize("status", [1, "1", 2, "2", None, True, False, 0, 99, "bad"])
async def test_sdk_online_policy_is_shared_by_every_family(
    hass: HomeAssistant, producer: Producer, status: object
) -> None:
    """All cached selectors share the SDK policy; health never follows truthiness."""
    api, records, calls = producer
    for raw in records.values():
        raw["deviceInfos"]["status"] = status
    coordinator = coordinator_for(hass, api)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    snapshot = coordinator.inventory_snapshot
    assert snapshot is not None
    for serial, state in snapshot.devices.items():
        assert coordinator.device_state(serial) is state
        assert device_available(coordinator, serial) is (state.online is True)
        assert coordinator.device_data(serial) == state.as_dict()
    assert isinstance(coordinator.device_state("CAM001"), CameraState)
    assert isinstance(coordinator.device_state("DET001"), DetectorState)
    assert isinstance(coordinator.device_state("LIGHT1"), LightState)
    assert isinstance(coordinator.device_state("PLUG01"), PlugState)
    assert coordinator.devices_of_kind("plug") == {"PLUG01": snapshot.devices["PLUG01"]}
    assert coordinator.device_state("unknown") is None
    assert coordinator.device_data("unknown") == {}
    assert not device_available(coordinator, "unknown")
    assert len(calls) == 1  # Selectors and properties cannot perform another poll.
    await coordinator.async_close()


async def test_real_disappearance_render_failure_and_poll_recovery(
    hass: HomeAssistant, producer: Producer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained rows stay inspectable, never available until the producer recovers."""
    api, records, _ = producer
    coordinator = coordinator_for(hass, api)
    assert coordinator.device_state("CAM001") is None
    assert coordinator.devices_of_kind("camera") == {}
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    camera = EzvizEntity(coordinator, "CAM001")
    detector = EzvizInventoryEntity(coordinator, "DET001")
    first = coordinator.inventory_snapshot
    removed = records.pop("DET001")
    with monkeypatch.context() as patch:
        patch.setattr(
            factory, "camera_status", MagicMock(side_effect=ValueError("synthetic"))
        )
        coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert coordinator.inventory_snapshot is not None
    assert "CAM001" in coordinator.data  # Real SDK retains the prior row.
    assert not coordinator.inventory_snapshot.health["CAM001"].current
    assert not coordinator.inventory_snapshot.health["DET001"].present
    assert not camera.available and not detector.available
    assert device_available(coordinator, "LIGHT1")
    assert first is not None and first.health["CAM001"].current
    records["DET001"] = removed
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert camera.available and detector.available
    with monkeypatch.context() as patch:

        def fail(_categories: object) -> None:
            raise PyEzvizError("synthetic poll failure")

        patch.setattr(api, "get_device_records", fail)
        await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not camera.available and not detector.available
    coordinator.merge_mqtt_update("CAM001", {"ext": {"image": "synthetic"}})
    assert not camera.available  # Partial push is not a successful account poll.
    await coordinator.async_refresh()
    assert camera.available and detector.available
    await coordinator.async_close()


async def test_camera_overlay_is_detached_and_closing_rejects_push(
    hass: HomeAssistant, producer: Producer
) -> None:
    """A push cannot mutate typed state, revive health or outlive its owner."""
    api, _, calls = producer
    coordinator = coordinator_for(hass, api)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    snapshot = coordinator.inventory_snapshot
    assert snapshot is not None
    camera = EzvizEntity(coordinator, "CAM001")
    before = camera.data
    state = snapshot.devices["CAM001"]
    coordinator.merge_mqtt_update("CAM001", {"ext": {"image": "synthetic"}})
    assert camera.data is coordinator.device_data("CAM001")
    assert camera.data is not before
    assert camera.data["Motion_Trigger"] is True
    assert before == snapshot.devices["CAM001"].as_dict()
    assert coordinator.inventory_snapshot.devices["CAM001"] is state
    coordinator.merge_mqtt_update("unknown", {"ext": {"image": "synthetic"}})
    assert "unknown" not in coordinator.data
    coordinator.begin_shutdown()
    overlay = coordinator.data
    coordinator.merge_mqtt_update("CAM001", {"ext": {"image": "late"}})
    assert coordinator.data is overlay
    assert camera.data["last_alarm_pic"] == "synthetic"
    assert len(calls) == 1
    await coordinator.async_close()


async def test_shutdown_revokes_availability_for_all_families(
    hass: HomeAssistant, producer: Producer
) -> None:
    """An otherwise healthy accepted inventory is not available after shutdown."""
    api, _, _ = producer
    coordinator = coordinator_for(hass, api)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    serials = tuple(coordinator.inventory_data)
    assert all(device_available(coordinator, serial) for serial in serials)
    coordinator.begin_shutdown()
    assert coordinator.last_update_success
    assert all(not device_available(coordinator, serial) for serial in serials)
    await coordinator.async_close()
