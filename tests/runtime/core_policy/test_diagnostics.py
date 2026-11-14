"""Cached diagnostics through real HA entries, registries and JSON boundaries."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import make_mocked_request
import custom_components
from custom_components.ezviz_cloud import diagnostics, mqtt
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.runtime import EzvizConfigEntry, RuntimeCleanup
from pyezvizapi.devices.snapshot import (
    DeviceCapabilities,
    DeviceHealth,
    DeviceSnapshot,
    DeviceState,
)
import pytest

from homeassistant import loader
from homeassistant.components import diagnostics as ha_diagnostics, http
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from tests.runtime.test_hardening import entry_for

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "diagnostics.json"
type Inventory = tuple[
    EzvizConfigEntry, EzvizDataUpdateCoordinator, MagicMock, dr.AnyDeviceEntry
]


@pytest.fixture
def inventory(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> Inventory:
    """Build cached SDK state and real registry identities with synthetic secrets."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "timeout": 42,
            "cameras": {
                "PRIVATE_CAMERA": {
                    "username": "PRIVATE_USERNAME",
                    "password": "PRIVATE_PASSWORD",
                    "enc_key": "PRIVATE_KEY",
                    "rtsp_uses_verification_code": True,
                    "ip": "PRIVATE_IP",
                    "ffmpeg_arguments": "PRIVATE_RTSP_PATH",
                    "future_secret": {"payload": "PRIVATE_NESTED"},
                }
            },
        },
    )
    api = MagicMock()
    coordinator = EzvizDataUpdateCoordinator(
        hass, config_entry=entry, api=api, api_timeout=42
    )
    values = {
        "device_category": "IPC",
        "device_sub_category": "CP8",
        "version": "1.2.3",
        "status": 1,
        "battery_level": 0,
        "serial": "PRIVATE_CAMERA",
        "name": "PRIVATE_NAME",
        "last_alarm_pic": "PRIVATE_IMAGE_URL",
        "mac_address": "PRIVATE_MAC",
        "session_id": "PRIVATE_SESSION",
        "last_alarm_time": "PRIVATE_ACTIVITY_TIME",
        "url": "PRIVATE_URL",
        "supportExt": {"private": "PRIVATE_SECRET"},
    }
    states = {
        "PRIVATE_CAMERA": DeviceState(
            "PRIVATE_CAMERA",
            "camera",
            values,
            DeviceCapabilities(
                frozenset(
                    {"status", "battery_level", "encrypted", "PRIVATE_CAPABILITY"}
                ),
                {"door_open": False, "PRIVATE_BINARY": True},
            ),
        ),
        "PRIVATE_DETECTOR": DeviceState("PRIVATE_DETECTOR", "detector", {"status": 2}),
        "PRIVATE_LIGHT": DeviceState("PRIVATE_LIGHT", "light", {"status": True}),
        "PRIVATE_PLUG": DeviceState("PRIVATE_PLUG", "plug", {"status": "1"}),
    }
    coordinator.inventory_snapshot = DeviceSnapshot(
        7,
        NOW - timedelta(seconds=30),
        states,
        {
            "PRIVATE_CAMERA": DeviceHealth(True, True, NOW - timedelta(seconds=30)),
            "PRIVATE_DETECTOR": DeviceHealth(
                True, False, NOW - timedelta(seconds=120), "PyEzvizError"
            ),
            "PRIVATE_MISSING": DeviceHealth(False, False, None, "MissingFromInventory"),
        },
    )
    coordinator.inventory_data = coordinator.inventory_snapshot.as_dict()
    coordinator.data = {
        "PRIVATE_CAMERA": {**values, "battery_level": 51}
    }  # Newer push projection.
    coordinator.last_update_success = True
    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    registry = dr.async_get(hass)
    camera = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "PRIVATE_CAMERA")},
        name="PRIVATE_DEVICE_NAME",
        connections={("mac", "00:11:22:33:44:55")},
    )
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "PRIVATE_REGISTRY_ONLY")}
    )
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "camera", DOMAIN, "PRIVATE_ENTITY", config_entry=entry, device_id=camera.id
    )
    entity_registry.async_get_or_create(
        "image",
        DOMAIN,
        "PRIVATE_DISABLED",
        config_entry=entry,
        device_id=camera.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    return entry, coordinator, api, camera


async def test_account_snapshot_is_useful_private_and_detached(
    hass: HomeAssistant,
    inventory: Inventory,
) -> None:
    """Preserve useful cached fields while omitting secrets and shared references."""
    entry, coordinator, api, _ = inventory
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result == json.loads(SNAPSHOT.read_text())
    encoded = json.dumps(result, allow_nan=False)
    for secret in ("PRIVATE_", entry.entry_id, "00:11:22:33:44:55"):
        assert secret not in encoded
    api.assert_not_called()
    assert (
        api.mock_calls == []
    )  # No lazy SDK access, token read, poll or device request.
    camera = result["devices"][0]
    assert camera["details"]["battery_level"] == 51
    assert camera["capabilities"]["observed"]["encrypted"] is True
    assert camera["capabilities"]["observed"]["is_on"] is None
    assert (
        camera["capabilities"]["verified_binary_state_available"]["door_open"] is True
    )
    camera["details"]["version"] = "changed"
    assert coordinator.data["PRIVATE_CAMERA"]["version"] == "1.2.3"


async def test_device_download_only_contains_selected_owned_device(
    hass: HomeAssistant,
    inventory: Inventory,
) -> None:
    """Never include unrelated or foreign devices in a device download."""
    entry, _, _, camera = inventory
    account = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    selected = await diagnostics.async_get_device_diagnostics(hass, entry, camera)
    assert selected["devices"] == [account["devices"][0]]
    other_entry = entry_for(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, "PRIVATE_FOREIGN")},
    )
    assert (await diagnostics.async_get_device_diagnostics(hass, entry, foreign))[
        "devices"
    ] == []
    unknown = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("other", "PRIVATE_FOREIGN")},
    )
    assert (await diagnostics.async_get_device_diagnostics(hass, entry, unknown))[
        "devices"
    ] == []


async def test_unloaded_entry_has_no_runtime_or_private_data(
    hass: HomeAssistant,
) -> None:
    """An unloaded account must not read credentials or attempt setup."""
    entry = entry_for(hass)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result == {
        "schema_version": 2,
        "entry": {"state": "not_loaded", "version": 4, "minor_version": 1},
        "runtime_available": False,
        "devices": [],
    }


@pytest.mark.parametrize(
    ("sub_type", "device_count"),
    [pytest.param(None, None, id="account"), pytest.param("device", 1, id="device")],
)
async def test_actual_ha_download_view_serializes_snapshot(
    hass: HomeAssistant,
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    sub_type: str | None,
    device_count: int | None,
) -> None:
    """Exercise HA dispatch, device ownership and its downloadable JSON envelope."""
    entry, _, api, camera = inventory
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    hass.data[ha_diagnostics.DOMAIN] = ha_diagnostics.DiagnosticsData()
    # Use HA handler registration without starting an HTTP server.
    ha_diagnostics._register_diagnostics_platform(hass, DOMAIN, diagnostics)
    # Only host/system metadata is synthetic, not the download view/serializer.
    monkeypatch.setattr(
        ha_diagnostics,
        "async_get_system_info",
        AsyncMock(return_value={"user": "fixture"}),
    )
    monkeypatch.setattr(
        ha_diagnostics,
        "async_get_custom_components",
        AsyncMock(return_value={DOMAIN: integration}),
    )
    request = make_mocked_request(
        "GET", "/api/diagnostics/config_entry/fixture", app={http.KEY_HASS: hass}
    )
    request["hass_user"] = SimpleNamespace(is_admin=True)
    response = await ha_diagnostics.DownloadDiagnosticsView().get(
        request, "config_entry", entry.entry_id, sub_type, camera.id
    )
    assert response.status == 200
    assert response.content_type == "application/json"
    downloaded = json.loads(response.text)
    expected = json.loads(SNAPSHOT.read_text())
    expected["devices"] = expected["devices"][:device_count]
    assert downloaded["data"] == expected
    assert downloaded["integration_manifest"]["domain"] == DOMAIN
    assert "PRIVATE_" not in response.text
    assert api.mock_calls == []


async def test_failed_poll_and_pending_cleanup_remain_visible(
    hass: HomeAssistant,
    inventory: Inventory,
) -> None:
    """Report retained work without leaking a failed poll exception message."""
    entry, coordinator, _, _ = inventory
    coordinator.last_update_success = False
    coordinator.last_exception = TimeoutError("PRIVATE_EXCEPTION_MESSAGE")
    future = asyncio.get_running_loop().create_future()
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    # Model retained owners without starting SDK workers.
    coordinator._poll_future = future
    coordinator._action_futures = {future, done}
    entry.runtime_data._workers = {future, done}
    coordinator.begin_shutdown()
    entry.runtime_data.cleanup_error_type = "OSError"
    try:
        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        assert result["polling"]["last_exception_type"] == "TimeoutError"
        assert result["polling"]["snapshot_age_seconds"] == 30
        assert result["polling"]["in_flight"] is True
        assert result["lifecycle"] == {
            "shutting_down": True,
            "pending_actions": 1,
            "pending_setup_workers": 1,
            "cleanup_complete": False,
            "cleanup_error_type": "OSError",
        }
        assert all(not device["available"] for device in result["devices"])
        assert "PRIVATE_" not in json.dumps(result)
    finally:
        future.set_result(None)


async def test_empty_and_legacy_cache_without_runtime_owner_fields(
    hass: HomeAssistant,
    inventory: Inventory,
) -> None:
    """Keep legacy caches serializable and tolerate absent runtime-owner fields."""
    entry, coordinator, _, _ = inventory
    coordinator.inventory_snapshot = None
    coordinator.inventory_data = {}
    coordinator.data = {
        "PRIVATE_CAMERA": {
            "status": 2,
            "version": {"password": "PRIVATE_SECRET"},
            "battery_level": float("nan"),
        }
    }
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, push=None)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["polling"]["snapshot_age_seconds"] is None
    assert result["lifecycle"]["pending_setup_workers"] is None
    assert result["devices"][0]["online"] is None
    assert result["devices"][0]["available"] is False
    assert result["devices"][0]["details"] == {
        "status": 2,
        "version": None,
        "battery_level": None,
    }
    coordinator.data = {}
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["devices"][0]["details"] == {}
    assert "PRIVATE_" not in json.dumps(result, allow_nan=False)


def _raise_push_error() -> dict[str, str]:
    """Fail the SDK boundary without accessing a device or network."""
    raise OSError("PRIVATE_EXCEPTION_MESSAGE")


@pytest.mark.parametrize(
    ("getter", "expected"),
    [
        pytest.param(lambda: {"state": "online"}, {"state": "online"}, id="healthy"),
        pytest.param(
            _raise_push_error,
            {"state": "diagnostics_failed", "error_type": "OSError"},
            id="failed",
        ),
    ],
)
async def test_push_diagnostics_are_off_loop_and_failure_is_reported(
    hass: HomeAssistant,
    inventory: Inventory,
    getter: Callable[[], dict[str, str]],
    expected: dict[str, str],
) -> None:
    """A blocking or failed push getter must not lose the cached account report."""
    entry, _, _, _ = inventory
    main_thread = get_ident()

    def get_diagnostics() -> dict[str, str]:
        assert get_ident() != main_thread
        return getter()

    entry.runtime_data.push = SimpleNamespace(get_diagnostics=get_diagnostics)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["push"] == expected
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize(
    ("timestamp", "expected_age"),
    [
        pytest.param(None, None, id="unknown"),
        pytest.param(datetime(2026, 1, 1), None, id="naive"),
        pytest.param(NOW + timedelta(seconds=60), 0, id="clock-skew"),
    ],
)
async def test_unknown_and_future_freshness(
    hass: HomeAssistant,
    inventory: Inventory,
    timestamp: datetime | None,
    expected_age: int | None,
) -> None:
    """Export unknown/skewed health ages and finite JSON numbers safely."""
    entry, coordinator, _, _ = inventory
    coordinator.inventory_snapshot = DeviceSnapshot(
        8,
        NOW,
        {},
        {"PRIVATE_CAMERA": DeviceHealth(True, True, timestamp)},
    )
    fraction = 1.5
    coordinator.data["PRIVATE_CAMERA"]["battery_level"] = fraction
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["devices"][0]["health"]["last_success_age_seconds"] == expected_age
    assert result["devices"][0]["details"]["battery_level"] == fraction
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "sdk",
    [
        pytest.param(None, id="released"),
        pytest.param(
            SimpleNamespace(
                get_push_diagnostics=lambda: {
                    "state": "fatal",
                    "token": "PRIVATE_TOKEN",
                    "push_state": {"secret": "PRIVATE_KEY"},
                }
            ),
            id="retained",
        ),
    ],
)
def test_push_recovery_metadata_survives_retained_sdk_client(
    hass: HomeAssistant,
    sdk: SimpleNamespace | None,
) -> None:
    """Retain recovery metadata whether cleanup released or retained the worker."""
    handler = mqtt.EzvizMqttHandler(hass, MagicMock(), "PRIVATE_ENTRY")
    # Seed terminal metadata independently of SDK cleanup.
    handler._fatal_error_type = "EzvizTokenPersistenceError"
    handler._fatal_failure_category = "storage"
    handler._reauth_requested = True
    handler._mqtt = sdk
    result = handler.get_diagnostics()
    assert result["state"] == "fatal"
    assert result["reauth_requested"] is True
    assert result["fatal_failure_category"] == "storage"
    assert "PRIVATE_" not in json.dumps(result)
