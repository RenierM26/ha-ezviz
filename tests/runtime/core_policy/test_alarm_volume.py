"""Alarm volume from reported ChimeMusic, adapted from @blankcan's PR #229."""

from copy import deepcopy
from datetime import timedelta
import json
import logging
from threading import get_ident
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components.ezviz_cloud import number
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from pyezvizapi import EzvizClient
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.models import EzvizDeviceRecord
import pytest

from homeassistant import loader
from homeassistant.components.number import NumberEntity
from homeassistant.components.number.const import DATA_COMPONENT
from homeassistant.components.number.services import async_setup_services
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.core_policy.test_state_boundary import coordinator_for
from tests.runtime.test_hardening import entities

DESCRIPTION = next(
    item for item in number.STATIC_NUMBER_DESCRIPTIONS if item.key == "alarm_volume"
)
CHIME = {
    "volume": 25,
    "doorbell": 3,
    "pir": 2,
    "doorbell_enable": True,
    "pir_enable": False,
    "future": {"tones": [1, 2]},
}


def camera_data(value: object) -> dict[str, Any]:
    """Keep vendor fields synthetic and include an independent microphone config."""
    return {
        "channelNo": 2,
        "supportExt": {"185": "1"},
        "device_category": "IPC",
        "STATUS": {
            "optionals": {
                "ChimeMusic": value,
                "CustomVoice_Volume": {"volume": 60, "microphone_volume": 15},
            }
        },
    }


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("volume", [0, 25, 100])
def test_reported_integer_volume_and_complete_payload_are_preserved(
    encoded: bool, volume: int
) -> None:
    """Retain the original PR's zero support, clamp and payload-preservation case."""
    config = {**deepcopy(CHIME), "volume": volume}
    data = camera_data(json.dumps(config) if encoded else config)
    before = deepcopy(data)
    api = MagicMock(spec=EzvizClient)
    assert DESCRIPTION.get_value(data) == volume
    assert number._is_desc_supported(data, DESCRIPTION)
    DESCRIPTION.set_value(api, "CAM001", 101, data)
    api.set_dev_config_kv.assert_called_once_with(
        "CAM001", 2, "ChimeMusic", {**config, "volume": 100}
    )
    assert data == before


@pytest.mark.parametrize(
    "config",
    [
        None,
        "bad JSON",
        "[]",
        "null",
        [],
        {},
        {"doorbell": 1},
        {"volume": None},
        {"volume": True},
        {"volume": False},
        {"volume": "25"},
        {"volume": 2.5},
        {"volume": -1},
        {"volume": 101},
    ],
)
def test_unsupported_or_malformed_volume_never_creates_or_writes_a_control(
    config: object,
) -> None:
    """An invalid current snapshot must also reject writes by an existing entity."""
    data = camera_data(config)
    api = MagicMock(spec=EzvizClient)
    assert DESCRIPTION.get_value(data) is None
    assert not number._is_desc_supported(data, DESCRIPTION)
    with pytest.raises(PyEzvizError, match="supported alarm chime volume"):
        DESCRIPTION.set_value(api, "CAM001", 50, data)
    api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_or_boolean_requests_never_reach_sdk(value: float) -> None:
    """Reject invalid requests before the executor attempts serialization."""
    api = MagicMock(spec=EzvizClient)
    with pytest.raises(PyEzvizError, match="finite number"):
        DESCRIPTION.set_value(api, "CAM001", value, camera_data(CHIME))
    api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize("channel", [None, 2, "3"])
def test_published_sdk_serializes_only_the_requested_chime_change(
    monkeypatch: pytest.MonkeyPatch, channel: int | str | None
) -> None:
    """Use SDK 2.2.0's real devconfig adapter, mocking only HTTP acquisition."""
    api = EzvizClient(token={"session_id": "synthetic", "api_url": "private.invalid"})
    transport = MagicMock(return_value={"meta": {"code": 200}})
    monkeypatch.setattr(api, "_request_json", transport)
    data = camera_data(deepcopy(CHIME))
    data["channelNo"] = channel
    try:
        DESCRIPTION.set_value(api, "CAM001", 38.7, data)
        args, kwargs = transport.call_args
        assert args[0] == "PUT"
        assert args[1].endswith(f"CAM001/{int(channel) if channel else 1}/op")
        assert kwargs["data"]["key"] == "ChimeMusic"
        assert json.loads(kwargs["data"]["value"]) == {**CHIME, "volume": 39}
        assert data["STATUS"]["optionals"]["ChimeMusic"] == CHIME
        assert transport.call_count == 1
    finally:
        api.close_session()


async def test_platform_creates_only_observed_controls_with_stable_identity(
    hass: HomeAssistant,
) -> None:
    """A zero volume still creates a configuration entity; unsupported rows do not."""
    entry, _, coordinator, *_ = entities(hass)
    coordinator.data["CAM001"].update(camera_data({**CHIME, "volume": 0}))
    coordinator.data["CAM002"] = {
        **coordinator.data["CAM001"],
        "STATUS": {"optionals": {}},
    }
    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    add = MagicMock()
    try:
        await number.async_setup_entry(hass, entry, add)
        actual = [
            item
            for item in add.call_args.args[0]
            if item.entity_description.key == "alarm_volume"
        ]
        assert len(actual) == 1
        entity = actual[0]
        assert isinstance(entity, NumberEntity)
        assert entity.unique_id == "CAM001_alarm_volume"
        assert entity.native_value == 0
        assert entity.entity_category is EntityCategory.CONFIG
        assert (
            entity.native_min_value,
            entity.native_max_value,
            entity.native_step,
        ) == (0, 100, 1)
        assert entity.translation_key == "alarm_volume"
    finally:
        await coordinator.async_close()


async def test_real_number_service_uses_owned_executor_and_refreshes_observed_state(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise HA service range validation, command scheduling and poll-owned state."""
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    entry, api, coordinator, *_ = entities(hass)
    coordinator.data["CAM001"].update(camera_data(deepcopy(CHIME)))
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
    worker_threads: list[int] = []

    def write(*_args: object) -> None:
        worker_threads.append(get_ident())

    api.set_dev_config_kv = MagicMock(side_effect=write)
    component = EntityComponent[NumberEntity](
        logging.getLogger(__name__), "number", hass
    )
    hass.data[DATA_COMPONENT] = component
    async_setup_services(hass)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="number",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    entity = number.EzvizNumber(coordinator, "CAM001", DESCRIPTION)
    try:
        await platform.async_add_entities([entity])
        assert hass.states.get(entity.entity_id).state == "25.0"
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity.entity_id, "value": 101},
                blocking=True,
            )
        api.set_dev_config_kv.assert_not_called()
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity.entity_id, "value": 80},
            blocking=True,
        )
        api.set_dev_config_kv.assert_called_once_with(
            "CAM001", 2, "ChimeMusic", {**CHIME, "volume": 80}
        )
        assert worker_threads and all(
            thread != get_ident() for thread in worker_threads
        )
        refresh.assert_awaited_once()
        assert entity.native_value == 25  # No optimistic alarm/volume confirmation.
        coordinator.data["CAM001"]["STATUS"]["optionals"]["ChimeMusic"]["volume"] = 80
        coordinator.async_set_updated_data(coordinator.data)
        assert entity.native_value == 80
        api.set_dev_config_kv.side_effect = PyEzvizError(
            "Denied: response code=403, fields=2"
        )
        refresh.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(45)
        refresh.assert_not_awaited()
        assert entity.native_value == 80
        coordinator.data["CAM001"]["STATUS"]["optionals"].pop("ChimeMusic")
        assert entity.native_value is None
        api.set_dev_config_kv.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(45)
        api.set_dev_config_kv.assert_not_called()
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize(("category", "current"), [("IPC", 25), ("DBScreen", 4)])
async def test_real_sdk_inventory_retains_the_volume_configuration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    encoded: bool,
    category: str,
    current: int,
) -> None:
    """Test the real SDK poll/projection rather than assuming raw STATUS is exposed."""
    api = EzvizClient(token={"session_id": "synthetic", "api_url": "private.invalid"})
    config = {**deepcopy(CHIME), "volume": current}
    raw = {
        "deviceInfos": {
            "deviceSerial": "CAM001",
            "deviceCategory": category,
            "deviceSubCategory": "Synthetic",
            "name": "Synthetic",
            "version": "1",
            "status": 1,
            "supportExt": {"185": "1", "203": "1"},
        },
        "WIFI": {},
        "SWITCH": {},
        "STATUS": {
            "optionals": {
                "ChimeMusic": json.dumps(config) if encoded else config,
                "CustomVoice_Volume": json.dumps(
                    {"volume": 60, "microphone_volume": 15}
                )
                if encoded
                else {"volume": 60, "microphone_volume": 15},
            }
        },
    }
    monkeypatch.setattr(
        api,
        "get_device_records",
        lambda _categories: {"CAM001": EzvizDeviceRecord.from_api("CAM001", raw)},
    )
    monkeypatch.setattr(
        api, "get_device_messages_list", lambda **_kwargs: {"messages": []}
    )
    coordinator = coordinator_for(hass, api)
    try:
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        data = coordinator.device_data("CAM001")
        if category == "DBScreen":
            # SDK 2.2.0 filters this category from the accepted camera projection.
            # HA range controls elsewhere are defensive, not new discovery support.
            assert data == {}
            assert not number._is_desc_supported(data, DESCRIPTION)
            return
        assert number._is_desc_supported(data, DESCRIPTION)
        assert DESCRIPTION.get_value(data) == current
        assert number._alarm_volume_config(data) == config
        assert number._alarm_voice_volume_getter(data) == 60
        assert number._alarm_volume_max(data) == (7 if category == "DBScreen" else 100)
    finally:
        await coordinator.async_close()
