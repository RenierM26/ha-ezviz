"""App-backed capability and scale separation for chime and alarm voice volume."""

from copy import deepcopy
from datetime import timedelta
import json
import logging
from threading import get_ident
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components.ezviz_cloud import number
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.exceptions import PyEzvizError
import pytest

from homeassistant import loader
from homeassistant.components.number import NumberEntity
from homeassistant.components.number.const import DATA_COMPONENT
from homeassistant.components.number.services import async_setup_services
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.core_policy.test_alarm_volume import CHIME, DESCRIPTION, camera_data
from tests.runtime.test_hardening import entities

VOICE = next(
    item
    for item in number.STATIC_NUMBER_DESCRIPTIONS
    if item.key == "alarm_voice_volume"
)
VOICE_CONFIG = {"volume": 60, "microphone_volume": 15, "future": {"mode": [1, 2]}}


@pytest.mark.parametrize(("description", "key"), [(DESCRIPTION, "185"), (VOICE, "203")])
@pytest.mark.parametrize(
    "flag", [None, "", "0", 0, "-1", -1, False, True, "false", "1,2", [], {}, 1.0]
)
def test_disabled_or_malformed_flags_block_creation_and_existing_entity_writes(
    description: number.EzvizNumberEntityDescription,
    key: str,
    flag: object,
) -> None:
    """Valid cached volume data never proves a missing/disabled capability."""
    data = camera_data(deepcopy(CHIME))
    data["supportExt"] = {} if flag is None else {key: flag}
    api = MagicMock(spec=EzvizClient)
    assert not number._is_desc_supported(data, description)
    assert description.get_value(data) is None
    with pytest.raises(PyEzvizError, match="supported"):
        description.set_value(api, "CAM001", 50, data)
    api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("flag", [1, "1", " 1 ", "01"])
def test_top_level_and_nested_enabled_capabilities_are_independent(
    nested: bool,
    flag: int | str,
) -> None:
    """203 alone cannot enable ChimeMusic, nor 185 alone enable voice volume."""
    for key, expected in [("185", DESCRIPTION), ("203", VOICE)]:
        data = camera_data(deepcopy(CHIME))
        data.pop("supportExt")
        if nested:
            data["deviceInfos"] = {"supportExt": {key: flag}}
        else:
            data["supportExt"] = {key: flag}
        for description in (DESCRIPTION, VOICE):
            assert number._is_desc_supported(data, description) is (
                description is expected
            )


def test_enabled_ring_variants_do_not_enable_unknown_voice_variants() -> None:
    """App RN chime gate is positive 185; native voice gate is exactly 203 == 1."""
    data = camera_data(deepcopy(CHIME))
    data["supportExt"] = {"185": "2", "203": "2", "550": "2", "735": "1"}
    assert number._is_desc_supported(data, DESCRIPTION)
    assert not number._is_desc_supported(data, VOICE)
    data["supportExt"] = {"550": "2", "735": "1"}
    assert not number._is_desc_supported(data, DESCRIPTION)


@pytest.mark.parametrize(
    ("category", "current", "maximum"), [("IPC", 25, 100), ("DBScreen", 4, 7)]
)
async def test_platform_separates_two_controls_and_uses_category_range(
    hass: HomeAssistant,
    category: str,
    current: int,
    maximum: int,
) -> None:
    """Existing chime identity is stable; the voice entity has a separate new ID."""
    entry, _, coordinator, *_ = entities(hass)
    data = camera_data({**CHIME, "volume": current})
    data.update({"supportExt": {"185": "1", "203": "1"}, "device_category": category})
    coordinator.data["CAM001"].update(data)
    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    add = MagicMock()
    try:
        await number.async_setup_entry(hass, entry, add)
        actual = {item.unique_id: item for item in add.call_args.args[0]}
        chime = actual["CAM001_alarm_volume"]
        voice = actual["CAM001_alarm_voice_volume"]
        assert chime.native_max_value == maximum
        assert chime.max_value == maximum
        assert chime.native_value == current
        assert chime.native_min_value == 0
        assert voice.native_min_value == 10
        assert voice.native_max_value == 100
        assert voice.native_step == 1
        assert voice.native_value == 60
        assert voice.translation_key == "alarm_voice_volume"
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("volume", [0, 7, 8, 100])
def test_screen_scale_applies_to_observation_and_direct_writes(volume: int) -> None:
    """Never expose a 0-100 slider for the app's DBScreen 0-7 configuration."""
    data = camera_data({**CHIME, "volume": volume})
    data["device_category"] = "DBScreen"
    api = MagicMock(spec=EzvizClient)
    assert number._is_desc_supported(data, DESCRIPTION) is (volume <= 7)
    if volume <= 7:
        DESCRIPTION.set_value(api, "CAM001", 80, data)
        api.set_dev_config_kv.assert_called_once_with(
            "CAM001", 2, "ChimeMusic", {**CHIME, "volume": 7}
        )
    else:
        with pytest.raises(PyEzvizError):
            DESCRIPTION.set_value(api, "CAM001", 4, data)
        api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("channel", [None, "3"])
def test_voice_real_sdk_adapter_preserves_mic_unknown_fields_and_other_chime(
    monkeypatch: pytest.MonkeyPatch,
    encoded: bool,
    channel: str | None,
) -> None:
    """Mock only HTTP: prove correct devconfig key, channel and complete payload."""
    data = camera_data(deepcopy(CHIME))
    data["supportExt"] = {"203": "1"}
    data["channelNo"] = channel
    data["STATUS"]["optionals"]["CustomVoice_Volume"] = (
        json.dumps(VOICE_CONFIG) if encoded else deepcopy(VOICE_CONFIG)
    )
    before = deepcopy(data)
    api = EzvizClient(token={"session_id": "synthetic", "api_url": "private.invalid"})
    transport = MagicMock(return_value={"meta": {"code": 200}})
    monkeypatch.setattr(api, "_request_json", transport)
    try:
        VOICE.set_value(api, "CAM001", 39.4, data)
        args, kwargs = transport.call_args
        assert args[0] == "PUT"
        assert args[1].endswith(f"CAM001/{channel or '1'}/op")
        assert kwargs["data"]["key"] == "CustomVoice_Volume"
        assert json.loads(kwargs["data"]["value"]) == {**VOICE_CONFIG, "volume": 39}
        assert data == before
        assert transport.call_count == 1
    finally:
        api.close_session()


@pytest.mark.parametrize(
    "config",
    [
        None,
        "bad",
        "[]",
        [],
        {},
        {"microphone_volume": 60},
        {"volume": True},
        {"volume": "60"},
        {"volume": 1},
        {"volume": 9},
        {"volume": 101},
    ],
)
def test_voice_capability_requires_its_own_valid_payload(config: object) -> None:
    """Never substitute ChimeMusic, a microphone reading or a default volume."""
    data = camera_data(deepcopy(CHIME))
    data["supportExt"] = {"203": "1"}
    data["STATUS"]["optionals"]["CustomVoice_Volume"] = config
    api = MagicMock(spec=EzvizClient)
    assert not number._is_desc_supported(data, VOICE)
    with pytest.raises(PyEzvizError):
        VOICE.set_value(api, "CAM001", 50, data)
    api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize("value", [0, 9, 101, float("nan"), float("inf"), True])
def test_voice_rejects_requests_outside_app_slider_range(value: float) -> None:
    """Muting belongs to sound mode; the voice slider selects 10-100 only."""
    data = camera_data(deepcopy(CHIME))
    data["supportExt"] = {"203": "1"}
    api = MagicMock(spec=EzvizClient)
    with pytest.raises(PyEzvizError, match="10 and 100"):
        VOICE.set_value(api, "CAM001", value, data)
    api.set_dev_config_kv.assert_not_called()


@pytest.mark.parametrize(
    ("description", "key", "minimum", "current", "accepted", "rejected", "maximum"),
    [
        (DESCRIPTION, "185", 0, 4, 7, 8, 7),
        (VOICE, "203", 10, 0, 50, 0, 100),
    ],
)
async def test_real_service_ranges_executor_errors_and_runtime_capability_loss(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    description: number.EzvizNumberEntityDescription,
    key: str,
    minimum: int,
    current: int,
    accepted: int,
    rejected: int,
    maximum: int,
) -> None:
    """Use HA's actual number dispatcher, including a reported muted voice value."""
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    entry, api, coordinator, *_ = entities(hass)
    data = camera_data({**CHIME, "volume": current})
    data["device_category"] = "DBScreen"
    data["supportExt"] = {key: "1"}
    data["STATUS"]["optionals"]["CustomVoice_Volume"] = {
        **VOICE_CONFIG,
        "volume": current,
    }
    coordinator.data["CAM001"].update(data)
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
    threads: list[int] = []

    def write(*_args: object) -> None:
        threads.append(get_ident())

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
    entity = number.EzvizNumber(coordinator, "CAM001", description)
    try:
        await platform.async_add_entities([entity])
        state = hass.states.get(entity.entity_id)
        assert state is not None and state.state == str(float(current))
        assert (state.attributes["min"], state.attributes["max"]) == (minimum, maximum)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity.entity_id, "value": rejected},
                blocking=True,
            )
        api.set_dev_config_kv.assert_not_called()
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity.entity_id, "value": accepted},
            blocking=True,
        )
        assert threads and all(thread != get_ident() for thread in threads)
        assert entity.native_value == current
        refresh.assert_awaited_once()
        failure = PyEzvizError("Denied: response code=403, fields=2")
        api.set_dev_config_kv.side_effect = failure
        refresh.reset_mock()
        with pytest.raises(HomeAssistantError) as caught:
            await entity.async_set_native_value(accepted)
        assert caught.value.__cause__ is failure
        refresh.assert_not_awaited()
        coordinator.data["CAM001"]["supportExt"][key] = "0"
        assert entity.native_value is None
        api.set_dev_config_kv.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(accepted)
        api.set_dev_config_kv.assert_not_called()
        refresh.assert_not_awaited()
    finally:
        await platform.async_reset()
        await coordinator.async_close()


def test_raw_nested_device_metadata_retains_the_screen_scale() -> None:
    """The nested metadata fallback must not accidentally turn a screen into 0-100."""
    data = camera_data({**CHIME, "volume": 7})
    data.pop("device_category")
    data.pop("supportExt")
    data["deviceInfos"] = {"deviceCategory": "DBScreen", "supportExt": {"185": "1"}}
    assert number._alarm_volume_max(data) == 7
    assert number._is_desc_supported(data, DESCRIPTION)
    api = MagicMock(spec=EzvizClient)
    DESCRIPTION.set_value(api, "CAM001", 80, data)
    api.set_dev_config_kv.assert_called_once_with(
        "CAM001", 2, "ChimeMusic", {**CHIME, "volume": 7}
    )


@pytest.mark.parametrize("speaker", [None, 60])
@pytest.mark.parametrize("encoded", [False, True])
async def test_microphone_service_preserves_siblings_and_uses_sdk_worker(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    speaker: int | None,
    encoded: bool,
) -> None:
    """Exercise the published SDK writer through actual HA service dispatch."""
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    entry, api, coordinator, *_ = entities(hass)
    description = next(
        item
        for item in number.STATIC_NUMBER_DESCRIPTIONS
        if item.key == "microphone_volume"
    )
    config = {"microphone_volume": 15, "future": {"keep": [1]}}
    if speaker is not None:
        config["volume"] = speaker
    data = camera_data(deepcopy(CHIME))
    data["supportExt"][str(SupportExt.SupportAudioOnoff.value)] = "1"
    data["STATUS"]["optionals"]["CustomVoice_Volume"] = (
        json.dumps(config) if encoded else deepcopy(config)
    )
    coordinator.data["CAM001"].update(data)
    before = deepcopy(coordinator.data)
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
    entity = number.EzvizNumber(coordinator, "CAM001", description)
    try:
        await platform.async_add_entities([entity])
        assert entity.unique_id == "CAM001_microphone_volume"
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity.entity_id, "value": 30},
            blocking=True,
        )
        api.set_dev_config_kv.assert_called_once_with(
            "CAM001", 2, "CustomVoice_Volume", {**config, "microphone_volume": 30}
        )
        assert worker_threads and all(
            thread != get_ident() for thread in worker_threads
        )
        refresh.assert_awaited_once()
        assert entity.native_value == 15
        assert coordinator.data == before
        api.set_dev_config_kv.reset_mock()
        refresh.reset_mock()
        coordinator.data["CAM001"]["STATUS"]["optionals"].pop("CustomVoice_Volume")
        assert entity.native_value is None
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(30)
        api.set_dev_config_kv.assert_not_called()
        refresh.assert_not_awaited()
    finally:
        await platform.async_reset()
        await coordinator.async_close()
