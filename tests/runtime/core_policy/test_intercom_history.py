"""History baselines follow SDK history success, not camera rendering currency."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from unittest.mock import MagicMock

from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.event import EzvizIntercomEvent
from pyezvizapi import EzvizClient
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.models import EzvizDeviceRecord
import pytest

from homeassistant.config_entries import ConfigEntry, current_entry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.test_hardening import entry_for


def history_coordinator(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[ConfigEntry, EzvizDataUpdateCoordinator, dict[str, Any], MagicMock]:
    """Keep real SDK acquisition/render/history orchestration and fake only IO."""
    entry = entry_for(hass)
    raw: dict[str, Any] = {
        "deviceInfos": {
            "deviceCategory": "BDoorBell",
            "name": "Synthetic",
            "deviceSubCategory": "TEST",
            "version": "1",
            "status": 1,
        },
        "STATUS": {"optionals": {}},
        "resourceInfos": [{"resourceIdentifier": "Video", "localIndex": 0}],
    }
    client = EzvizClient(
        token={"session_id": "synthetic", "api_url": "api.example.invalid"}
    )
    monkeypatch.setattr(
        client,
        "get_device_records",
        lambda _: {"CAM001": EzvizDeviceRecord.from_api("CAM001", deepcopy(raw))},
    )
    # A known prefetched row prevents the renderer's separate alarm fallback IO.
    monkeypatch.setattr(
        client,
        "_prefetch_latest_camera_alarms",
        MagicMock(
            return_value={
                "CAM001": {
                    "deviceSerial": "CAM001",
                    "msgId": "synthetic-prefetch",
                    "ext": {"alarmType": 0},
                }
            }
        ),
    )
    history = MagicMock(side_effect=PyEzvizError("Synthetic history failure"))
    monkeypatch.setattr(client, "get_device_messages_list", history)
    context = current_entry.set(entry)
    try:
        coordinator = EzvizDataUpdateCoordinator(
            hass, config_entry=entry, api=client, api_timeout=5
        )
    finally:
        current_entry.reset(context)
    return entry, coordinator, raw, history


def ring(identifier: str, *, age: int = 0) -> dict[str, Any]:
    """Use the SDK's real unified-message normalization with synthetic values."""
    return {
        "deviceSerial": "CAM001",
        "msgId": identifier,
        "time": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        "ext": {"alarmType": 2701},
    }


def event_platform(hass: HomeAssistant, entry: ConfigEntry) -> EntityPlatform:
    """Exercise actual EventEntity state publication and coordinator availability."""
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
    return platform


@pytest.mark.parametrize("empty_window", [False, True])
async def test_first_successful_history_during_render_failure_is_not_replayed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, empty_window: bool
) -> None:
    """A retained doorbell must not publish pre-start history after recovery."""
    entry, coordinator, raw, history = history_coordinator(hass, monkeypatch)
    platform = event_platform(hass, entry)
    received = MagicMock()
    try:
        await coordinator.async_refresh()
        assert coordinator.inventory_snapshot.health["CAM001"].current
        assert coordinator.inventory_snapshot.intercom_events_current is False
        entity = EzvizIntercomEvent(coordinator, "CAM001", "ring")
        await platform.async_add_entities([entity])
        coordinator.intercom_events.subscribe("CAM001", received)

        old = ring("before-startup", age=180)
        raw["STATUS"] = "malformed"
        history.side_effect = None
        history.return_value = {"message": [] if empty_window else [old]}
        await coordinator.async_refresh()
        health = coordinator.inventory_snapshot.health["CAM001"]
        assert health.present and not health.current
        assert health.error_type == "TypeError"
        assert coordinator.inventory_snapshot.intercom_events_current is True
        received.assert_not_called()
        assert not entity.available

        raw["STATUS"] = {"optionals": {}}
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert entity.available
        assert "event_id" not in hass.states.get(entity.entity_id).attributes
        received.assert_not_called()

        new = ring("after-baseline")
        history.return_value = {"message": [new] if empty_window else [new, old]}
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert received.call_count == 1
        assert (
            hass.states.get(entity.entity_id).attributes["event_id"] == "after-baseline"
        )

        raw["STATUS"] = "malformed"
        await coordinator.async_refresh()
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "channel_type": 1,
                    "device_serial": "CAM001",
                    "msgId": "live-while-stale",
                    "alert_type_code": 2701,
                    "time": datetime.now(UTC).isoformat(),
                }
            },
        )
        assert received.call_count == 2
        assert received.call_args.args[0].event_id == "live-while-stale"
    finally:
        await platform.async_reset()
        await coordinator.async_close()


async def test_unsupported_retained_doorbell_does_not_seed_unqueried_history(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global history success cannot seed a serial excluded from the SDK query."""
    _, coordinator, raw, history = history_coordinator(hass, monkeypatch)
    received = MagicMock()
    try:
        await coordinator.async_refresh()
        coordinator.intercom_events.subscribe("CAM001", received)
        raw["deviceInfos"]["deviceCategory"] = "UnsupportedSynthetic"
        history.side_effect = None
        history.reset_mock()
        await coordinator.async_refresh()
        health = coordinator.inventory_snapshot.health["CAM001"]
        assert health.present and not health.current
        assert health.error_type == "UnsupportedCategory"
        assert coordinator.inventory_snapshot.intercom_events_current is True
        history.assert_not_called()

        raw["deviceInfos"]["deviceCategory"] = "BDoorBell"
        old = ring("first-queried-history", age=180)
        history.return_value = {"message": [old]}
        await coordinator.async_refresh()
        received.assert_not_called()
        history.return_value = {"message": [ring("new-history"), old]}
        await coordinator.async_refresh()
        assert received.call_count == 1
        assert received.call_args.args[0].event_id == "new-history"
    finally:
        await coordinator.async_close()


async def test_push_before_history_baseline_survives_stale_rendering(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push remains independent of history failure, baseline and render health."""
    _, coordinator, raw, history = history_coordinator(hass, monkeypatch)
    received = MagicMock()
    try:
        await coordinator.async_refresh()
        coordinator.intercom_events.subscribe("CAM001", received)
        raw["STATUS"] = "malformed"
        await coordinator.async_refresh()
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "device_serial": "CAM001",
                    "msgId": "push-before-baseline",
                    "alert_type_code": 2701,
                    "time": datetime.now(UTC).isoformat(),
                }
            },
        )
        assert received.call_count == 1
        history.side_effect = None
        history.return_value = {
            "message": [ring("push-before-baseline"), ring("old", age=180)]
        }
        await coordinator.async_refresh()
        assert received.call_count == 1
        raw["STATUS"] = {"optionals": {}}
        await coordinator.async_refresh()
        assert received.call_count == 1
    finally:
        await coordinator.async_close()
