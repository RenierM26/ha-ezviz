"""Real HA intercom discovery, commands, events and lifecycle without network IO."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any
from unittest.mock import MagicMock

from custom_components.ezviz_cloud import button, event, number, switch
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.intercom_events import event_zones
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from pyezvizapi import DeviceSnapshot, MonitorState
from pyezvizapi.devices.intercom_events import IntercomEvent
from pyezvizapi.devices.snapshot import CameraState, DeviceHealth
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.push.notifications import decode_notification
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.test_hardening import entities


def intercom(hass: HomeAssistant) -> tuple[Any, Any, EzvizDataUpdateCoordinator]:
    """Use real SDK snapshots, one station and an independently advertised monitor."""
    entry, api, coordinator, *_ = entities(hass)
    raw = {
        **coordinator.data["CAM001"],
        "device_category": "BDoorBell",
        "supportExt": {"662": "1"},
        "resourceInfos": [{"resourceIdentifier": "Video", "localIndex": 0}],
        "STATUS": {
            "optionals": {
                "ChimeMusic": {
                    "volume": 4,
                    "doorbell_enable": 1,
                    "pir_enable": 0,
                    "doorbell": 2,
                    "future": {"preserve": True},
                }
            }
        },
        "Motion_Trigger": False,
    }
    monitor = {**deepcopy(raw), "device_category": "DBScreen", "name": "Monitor"}
    coordinator.data = {"CAM001": raw}
    coordinator.inventory_data = {"CAM001": deepcopy(raw), "MON001": monitor}
    now = datetime.now(UTC)
    coordinator.inventory_snapshot = DeviceSnapshot(
        1,
        now,
        {
            "CAM001": CameraState("CAM001", "camera", raw),
            "MON001": MonitorState("MON001", "monitor", monitor),
        },
        {serial: DeviceHealth(True, True, now) for serial in ("CAM001", "MON001")},
        intercom_events_current=True,
    )
    coordinator.intercom_events.snapshot(coordinator.inventory_snapshot)
    api.set_dev_config_kv = MagicMock(return_value={"meta": {"code": 200}})
    api.remote_unlock = MagicMock(return_value=True)
    api.export_token = MagicMock(return_value={"username": "synthetic"})
    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    return entry, api, coordinator


def occurrence(
    identifier: str = "ID", kind: str = "ring", age: int = 0
) -> IntercomEvent:
    """Make a fresh numeric-code event with no picture or human identity."""
    return IntercomEvent(
        "CAM001",
        identifier,
        "ring" if kind == "ring" else "lock_unlocked",
        2701 if kind == "ring" else 10242,
        datetime.now(UTC) - timedelta(seconds=age),
        1,
    )


async def test_platform_discovers_monitors_without_cameras_or_duplicate_volume(
    hass: HomeAssistant,
) -> None:
    """Observe controls, stable IDs and no unlock/event entities on the monitor."""
    entry, _, coordinator = intercom(hass)
    add = MagicMock()
    try:
        for platform in (button, switch, number, event):
            await platform.async_setup_entry(hass, entry, add)
        actual = [entity for call in add.call_args_list for entity in call.args[0]]
        ids = [entity.unique_id for entity in actual]
        assert ids.count("CAM001_alarm_volume") == 1
        assert ids.count("MON001_alarm_volume") == 1
        assert {
            "CAM001_unlock_door",
            "CAM001_unlock_gate",
            "MON001_intercom_doorbell_enable",
        }.issubset(ids)
        assert not any(
            identifier.startswith(("MON001_unlock", "MON001_intercom_ring"))
            for identifier in ids
        )
        assert "MON001" not in coordinator.data
        for entity in actual:
            if isinstance(entity, number.EzvizIntercomVolume):
                assert entity.native_max_value == (
                    7 if entity.serial == "MON001" else 100
                )
                assert entity.native_value == 4
        # Re-notifying the same snapshot does not duplicate dynamic entities.
        add.reset_mock()
        coordinator.async_update_listeners()
        assert not add.called
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(("relay", "lock_no"), [("door", 2), ("gate", 1)])
async def test_unlock_uses_sdk_route_and_never_other_relay(
    hass: HomeAssistant, relay: str, lock_no: int
) -> None:
    """Rejections propagate once; there is no optimistic physical lock state."""
    _, api, coordinator = intercom(hass)
    entity = button.EzvizIntercomUnlock(coordinator, "CAM001", relay)
    try:
        await entity.async_press()
        assert api.remote_unlock.call_count == 1
        assert api.remote_unlock.call_args.args[2] == lock_no
        assert api.remote_unlock.call_args.kwargs["local_index"] == "0"
        api.remote_unlock.side_effect = PyEzvizError("rejected")
        with pytest.raises(HomeAssistantError):
            await entity.async_press()
        assert api.remote_unlock.call_count == 2
        coordinator.data["CAM001"]["supportExt"]["662"] = "0"
        api.remote_unlock.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entity.async_press()
        api.remote_unlock.assert_not_called()
    finally:
        await coordinator.async_close()


async def test_chime_siblings_survive_stale_poll_and_are_never_displayed_optimistically(
    hass: HomeAssistant,
) -> None:
    """Two successive commands preserve the accepted sibling in the real SDK write."""
    _, api, coordinator = intercom(hass)
    bell = switch.EzvizIntercomSwitch(coordinator, "CAM001", "doorbell_enable")
    volume = number.EzvizIntercomVolume(coordinator, "CAM001")
    try:
        await bell.async_turn_off()
        await volume.async_set_native_value(7)
        config = api.set_dev_config_kv.call_args.args[3]
        assert config["doorbell_enable"] == 0 and config["volume"] == 7
        assert config["future"] == {"preserve": True}
        assert bell.is_on is True and volume.native_value == 4
        coordinator.data["CAM001"]["STATUS"]["optionals"]["ChimeMusic"].pop(
            "doorbell_enable"
        )
        api.set_dev_config_kv.reset_mock()
        with pytest.raises(HomeAssistantError):
            await bell.async_turn_on()
        api.set_dev_config_kv.assert_not_called()
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(("serial", "maximum"), [("MON001", 7), ("CAM001", 100)])
@pytest.mark.parametrize("value", [-1, 101, 0.5, float("nan"), float("inf"), True])
async def test_invalid_volume_never_reaches_device(
    hass: HomeAssistant, serial: str, maximum: int, value: float
) -> None:
    """Validate direct calls as well as HA's service schema."""
    _, api, coordinator = intercom(hass)
    try:
        with pytest.raises(HomeAssistantError):
            await number.EzvizIntercomVolume(
                coordinator, serial
            ).async_set_native_value(value)
        with pytest.raises(HomeAssistantError):
            await number.EzvizIntercomVolume(
                coordinator, serial
            ).async_set_native_value(maximum + 1)
        api.set_dev_config_kv.assert_not_called()
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(("serial", "maximum"), [("MON001", 7), ("CAM001", 100)])
async def test_chime_volume_uses_category_range(
    hass: HomeAssistant, serial: str, maximum: int
) -> None:
    """HA accepts each app-backed upper bound through the real SDK setter."""
    _, api, coordinator = intercom(hass)
    volume = number.EzvizIntercomVolume(coordinator, serial)
    try:
        assert volume.native_max_value == maximum
        for value in (0, maximum):
            await volume.async_set_native_value(value)
            assert api.set_dev_config_kv.call_args.args[0] == serial
            assert api.set_dev_config_kv.call_args.args[3]["volume"] == value
        assert volume.native_value == 4
    finally:
        await coordinator.async_close()


async def test_monitor_uses_own_serial_and_health(hass: HomeAssistant) -> None:
    """No composite-serial splitting or accidental command to the outdoor station."""
    _, api, coordinator = intercom(hass)
    entity = number.EzvizIntercomVolume(coordinator, "MON001")
    try:
        await entity.async_set_native_value(2)
        assert api.set_dev_config_kv.call_args.args[0] == "MON001"
        assert entity.device_info == DeviceInfo(
            identifiers={(DOMAIN, "MON001")},
            manufacturer="EZVIZ",
            model="CP8",
            name="Monitor",
            sw_version="1",
            serial_number="MON001",
        )
        snap = coordinator.inventory_snapshot
        coordinator.inventory_snapshot = replace(
            snap, health={**snap.health, "MON001": DeviceHealth(False, False, None)}
        )
        assert not entity.available
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(3)
    finally:
        await coordinator.async_close()


async def test_initial_history_suppressed_and_push_history_deduplicated(
    hass: HomeAssistant,
) -> None:
    """The initial historical window is a baseline, not a new ring/unlock."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    remove = coordinator.intercom_events.subscribe("CAM001", received)
    try:
        coordinator.intercom_events.close()
        coordinator.intercom_events.subscribe("CAM001", received)
        snap = replace(
            coordinator.inventory_snapshot, intercom_events=(occurrence("initial"),)
        )
        coordinator.intercom_events.snapshot(snap)
        received.assert_not_called()
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "device_serial": "CAM001",
                    "msgId": "new",
                    "alert_type_code": 2701,
                    "time": datetime.now(UTC).isoformat(),
                }
            },
        )
        assert received.call_count == 1
        coordinator.intercom_events.snapshot(
            replace(snap, intercom_events=(occurrence("new"),))
        )
        assert received.call_count == 1
        assert coordinator.data["CAM001"]["Motion_Trigger"] is False
        coordinator.intercom_events.snapshot(
            replace(snap, intercom_events=(occurrence("second"),))
        )
        assert received.call_count == 2
        remove()
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("age", [301, -31])
async def test_old_and_future_pushes_are_not_live_events(
    hass: HomeAssistant, age: int
) -> None:
    """Reconnect replay and bad clocks never emit a fresh automation event."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    try:
        coordinator.intercom_events.snapshot(
            replace(
                coordinator.inventory_snapshot, intercom_events=(occurrence(age=age),)
            )
        )
        received.assert_not_called()
    finally:
        await coordinator.async_close()


async def test_event_entity_registry_rename_and_unload_revoke_subscription(
    hass: HomeAssistant,
) -> None:
    """Use real HA EventEntity/registry ownership; each message triggers once."""
    entry, _, coordinator = intercom(hass)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="event",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    entity = event.EzvizIntercomEvent(coordinator, "CAM001", "ring")
    try:
        await platform.async_add_entities([entity])
        coordinator.intercom_events.snapshot(
            replace(
                coordinator.inventory_snapshot, intercom_events=(occurrence("first"),)
            )
        )
        initial = hass.states.get(entity.entity_id)
        assert initial.attributes["event_type"] == "ring"
        assert initial.attributes["event_id"] == "first"
        er.async_get(hass).async_update_entity(
            entity.entity_id, new_entity_id="event.renamed_intercom"
        )
        await hass.async_block_till_done()
        coordinator.intercom_events.snapshot(
            replace(
                coordinator.inventory_snapshot, intercom_events=(occurrence("next"),)
            )
        )
        state = hass.states.get("event.renamed_intercom")
        assert state.attributes["event_id"] == "next"
        assert len(coordinator.intercom_events._listeners["CAM001"]) == 1
        await platform.async_reset()
        assert coordinator.intercom_events._listeners == {}
        coordinator.intercom_events.snapshot(
            replace(
                coordinator.inventory_snapshot,
                intercom_events=(occurrence("after-remove"),),
            )
        )
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("zone", [None, "", "Invalid/Timezone", "/etc/passwd", 2])
async def test_invalid_zone_push_is_unknown_until_authoritative_history(
    hass: HomeAssistant, zone: object
) -> None:
    """Never use host-local TZ for naive strings or poison later history dedup."""

    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    coordinator.intercom_events.snapshot(
        coordinator.inventory_snapshot, event_zones({"CAM001": {"cam_timezone": zone}})
    )
    try:
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "device_serial": "CAM001",
                    "msgId": "enriched",
                    "alert_type_code": 2701,
                    "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                }
            },
        )
        received.assert_not_called()
        coordinator.intercom_events.snapshot(
            replace(
                coordinator.inventory_snapshot,
                intercom_events=(occurrence("enriched"),),
            )
        )
        assert received.call_count == 1
    finally:
        await coordinator.async_close()


async def test_removed_device_cannot_emit_events(hass: HomeAssistant) -> None:
    """Retained old SDK rows do not turn a removed device into a live publisher."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    try:
        coordinator.inventory_snapshot = replace(
            coordinator.inventory_snapshot,
            health={"CAM001": DeviceHealth(False, False, None)},
        )
        coordinator.intercom_events.snapshot(
            replace(coordinator.inventory_snapshot, intercom_events=(occurrence(),))
        )
        received.assert_not_called()
    finally:
        await coordinator.async_close()


async def test_boolean_volume_rejected(hass: HomeAssistant) -> None:
    """A boolean is not an integer volume in direct entity calls."""
    _, api, coordinator = intercom(hass)
    try:
        with pytest.raises(HomeAssistantError):
            await number.EzvizIntercomVolume(
                coordinator, "CAM001"
            ).async_set_native_value(True)
        api.set_dev_config_kv.assert_not_called()
    finally:
        await coordinator.async_close()


async def test_label_light_rejection_retains_observed_state(
    hass: HomeAssistant,
) -> None:
    """Exercise the actual HA adapter and published SDK response boundary."""
    _, api, coordinator = intercom(hass)
    coordinator.data["CAM001"]["FEATURE_INFO"] = {
        "0": {"Video": {"LightCtrl": {"NightLightEnable": True}}}
    }
    entity = switch.EzvizIntercomSwitch(coordinator, "CAM001", "label_light")
    api.set_iot_feature = MagicMock(return_value={"meta": {"code": 200}})
    try:
        await entity.async_turn_off()
        assert api.set_iot_feature.call_args.kwargs["local_index"] == "0"
        assert api.set_iot_feature.call_args.kwargs["value"] == {"value": False}
        assert entity.is_on is True
        api.set_iot_feature.side_effect = PyEzvizError("response code=403, fields=2")
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_off()
        assert entity.is_on is True
        coordinator.data["CAM001"].pop("FEATURE_INFO")
        api.set_iot_feature.reset_mock()
        assert entity.is_on is None
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()
        api.set_iot_feature.assert_not_called()
    finally:
        await coordinator.async_close()


async def test_known_timezone_push_does_not_resolve_files_on_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll executor resolves zones; the synchronous push path uses its cache."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    zones = await hass.async_add_executor_job(
        event_zones, {"CAM001": {"cam_timezone": "UTC"}}
    )
    coordinator.intercom_events.snapshot(coordinator.inventory_snapshot, zones)
    monkeypatch.setattr(
        "custom_components.ezviz_cloud.intercom_events.dt_util.get_time_zone",
        MagicMock(side_effect=AssertionError("No timezone file IO in push")),
    )
    try:
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "device_serial": "CAM001",
                    "msgId": "utc-push",
                    "alert_type_code": 2701,
                    "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                }
            },
        )
        assert received.call_count == 1
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("code", "subtype", "kind"),
    [
        (0, 2701, "ring"),
        (None, "2701", "ring"),
        (2701, None, "ring"),
        (10242, None, "lock_unlocked"),
    ],
)
async def test_image_push_uses_sdk_normalization_without_motion(
    hass: HomeAssistant, code: int | None, subtype: int | str | None, kind: str
) -> None:
    """Image-bearing subtype fallback rings use the same event/motion decision."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    payload = {
        "ext": {
            "channel_type": 1,
            "device_serial": "CAM001",
            "msgId": "normalized-push",
            "alert_type_code": code,
            "subType": subtype,
            "customerType": "Card",
            "time": datetime.now(UTC).isoformat(),
            "image": "synthetic-picture",
        }
    }
    try:
        coordinator.merge_mqtt_update("CAM001", payload)
        assert received.call_count == 1
        normalized = received.call_args.args[0]
        assert normalized.event_type == kind
        assert normalized.method == ("rfid" if kind == "lock_unlocked" else None)
        assert coordinator.data["CAM001"]["Motion_Trigger"] is False
        assert coordinator.data["CAM001"]["last_alarm_pic"] == "synthetic-picture"
        coordinator.merge_mqtt_update("CAM001", payload)
        assert received.call_count == 1
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("family", ["14", "2", "99"])
async def test_non_alarm_sdk_notification_does_not_change_camera_state(
    hass: HomeAssistant, family: str
) -> None:
    """Call thumbnails and unknown CSV layouts are not camera motion alarms."""
    _, _, coordinator = intercom(hass)
    received = MagicMock()
    coordinator.intercom_events.subscribe("CAM001", received)
    payload = decode_notification(
        json.dumps(
            {
                "ext": ",".join(
                    [
                        family,
                        datetime.now(UTC).isoformat(),
                        "CAM001",
                        "0",
                        "2701",
                        "call-id",
                        "1",
                        "synthetic-picture",
                        "0",
                        "checksum",
                        "0",
                        "analysis",
                        "",
                        "Monitor",
                        "",
                        "custom",
                    ]
                )
            }
        ).encode()
    )
    original = deepcopy(coordinator.data)
    try:
        assert payload is not None
        coordinator.merge_mqtt_update("CAM001", payload)
        received.assert_not_called()
        assert coordinator.data == original
    finally:
        await coordinator.async_close()
