"""Regressions for independent review of effective cloud command state."""

import asyncio
from copy import deepcopy
import json
from threading import Event
from unittest.mock import MagicMock

from pyezvizapi.constants import SupportExt
import pytest

from custom_components.ezviz_cloud import number, select, setting_writes
from homeassistant.exceptions import HomeAssistantError

from .test_setting_writes import controls, snapshot


def configured(hass, *, encoded=False, mode=2, category="IPC", layout="STATUS"):
    _, api, data, *_ = controls(hass)
    configs = {
        "device_ICR_DSS": {"mode": 0, "sensitivity": 1},
        "NightVision_Model": {"graphicType": mode, "luminance": 40, "duration": 60, "future": [1]},
        "CustomVoice_Volume": {"volume": 60, "microphone_volume": 15, "future": [1]},
        "ChimeMusic": {"volume": 4, "enable": 1, "musicType": 2, "future": [1]},
        "AlgorithmInfo": [
            {"channel": 1, "SubType": subtype, "Value": 2} for subtype in ("0", "3", "4")
        ],
    }
    current = snapshot(
        data,
        device_category=category,
        channelNo="2",
        supportExt={
            "185": "1",
            "203": "1",
            str(SupportExt.SupportAudioOnoff.value): "1",
        },
        STATUS={
            "optionals": {
                key: json.dumps(value) if encoded else value for key, value in configs.items()
            }
        },
    )
    if layout != "STATUS":
        values = current.as_dict()["CAM001"]
        options = deepcopy(values["STATUS"]["optionals"])
        options = json.dumps(options) if encoded else options
        changes = (
            {"optionals": options}
            if layout == "optionals"
            else {"statusInfo": {"optionals": options}}
        )
        current = snapshot(data, **values, **changes)
    data.inventory_snapshot = current
    data.inventory_data = current.as_dict()
    data.data = {"CAM001": deepcopy(data.inventory_data["CAM001"])}
    api.set_dev_config_kv = MagicMock(return_value=True)
    api.set_detection_sensitivity = api.set_algorithm_param = MagicMock(return_value=True)
    return api, data


def selector(data, key):
    return select.EzvizSelect(
        data, "CAM001", next(desc for desc in select.SELECTS if desc.key == key)
    )


def numeric(data, key):
    return number.EzvizNumber(
        data, "CAM001", next(desc for desc in number.STATIC_NUMBER_DESCRIPTIONS if desc.key == key)
    )


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("layout", ["STATUS", "optionals", "statusInfo"])
async def test_day_night_writes_preserve_accepted_sibling_and_forced_auto_mode(
    hass, encoded, reverse, layout
):
    api, data = configured(hass, encoded=encoded, layout=layout)
    mode, sensitivity = selector(data, "day_night_mode"), selector(data, "day_night_sensitivity")
    before = deepcopy(data.data)
    try:
        if reverse:
            await mode.async_select_option("day_night_night")
            await sensitivity.async_select_option("day_night_sensitivity_high")
            assert mode.current_option == "day_night_auto"
            assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 0, "sensitivity": 3}
        else:
            await sensitivity.async_select_option("day_night_sensitivity_high")
            await mode.async_select_option("day_night_night")
            assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 2, "sensitivity": 3}
        assert sensitivity.current_option == "day_night_sensitivity_low"
        assert data.data == before
    finally:
        await data.async_close()


async def test_compound_context_is_prepared_after_queued_sibling_ack(hass):
    api, data = configured(hass)
    entered, release = Event(), Event()
    calls = []

    def wire(*args):
        calls.append(deepcopy(args[-1]))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return True

    api.set_dev_config_kv.side_effect = wire
    first = asyncio.create_task(
        selector(data, "day_night_sensitivity").async_select_option("day_night_sensitivity_high")
    )
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(
            selector(data, "day_night_mode").async_select_option("day_night_night")
        )
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert calls == [{"mode": 0, "sensitivity": 3}, {"mode": 2, "sensitivity": 3}]
    finally:
        release.set()
        await first
        if second:
            await second
        await data.async_close()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("layout", ["STATUS", "optionals", "statusInfo"])
async def test_audio_compound_writes_preserve_normalized_accepted_siblings(
    hass, encoded, reverse, layout
):
    api, data = configured(hass, encoded=encoded, layout=layout)
    voice, mic = numeric(data, "alarm_voice_volume"), numeric(data, "microphone_volume")
    before = deepcopy(data.data)
    try:
        first, second = (mic, voice) if reverse else (voice, mic)
        await first.async_set_native_value(39.4)
        await second.async_set_native_value(45.7)
        payload = api.set_dev_config_kv.call_args.args[-1]
        assert payload["microphone_volume"] == (39 if reverse else 46)
        assert payload["volume"] == (46 if reverse else 39)
        assert payload["future"] == [1]
        assert voice.native_value == 60
        assert mic.native_value == 15
        assert data.data == before
    finally:
        await data.async_close()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("layout", ["STATUS", "optionals", "statusInfo"])
async def test_night_vision_writes_and_mode_aliases_preserve_accepted_config(
    hass, encoded, reverse, layout
):
    api, data = configured(hass, encoded=encoded, layout=layout)
    luminance, duration = (
        numeric(data, "night_vision_luminance"),
        numeric(data, "night_vision_duration"),
    )
    before = deepcopy(data.data)
    try:
        if reverse:
            await duration.async_set_native_value(90.4)
            await luminance.async_set_native_value(75.6)
        else:
            await luminance.async_set_native_value(75.6)
            await duration.async_set_native_value(90.4)
        mode = selector(data, "smart_night_vision_model")
        await mode.async_select_option("night_vision_smart")
        payload = api.set_dev_config_kv.call_args.args[-1]
        assert payload == {"graphicType": 2, "luminance": 76, "duration": 90, "future": [1]}
        assert selector(data, "super_night_vision_model").current_option == "night_vision_smart"
        assert luminance.native_value == 40 and duration.native_value == 60
        assert data.data == before
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("detection_sensibility", 3.9, 3),
        ("algorithm_param_0_1", 3.9, 3),
        ("algorithm_param_3_1", 3.9, 3),
        ("algorithm_param_4_1", 3.9, 3),
        ("alarm_volume", 39.4, 39),
        ("alarm_volume", 101.8, 100),
        ("alarm_voice_volume", 39.6, 40),
        ("microphone_volume", 39.4, 39),
        ("microphone_volume", 101.8, 100),
        ("night_vision_luminance", 75.6, 76),
        ("night_vision_duration", 90.6, 91),
    ],
)
async def test_effective_numeric_target_matches_wire_and_fresh_sdk_observation(
    hass, key, value, expected
):
    api, data = configured(hass)
    entity = numeric(data, key)
    try:
        await entity.async_set_native_value(value)
        before = entity.native_value
        assert before != expected
        optionals = deepcopy(data.data["CAM001"]["STATUS"]["optionals"])
        if key.startswith("algorithm") or key == "detection_sensibility":
            position = -2 if key in ("algorithm_param_3_1", "algorithm_param_4_1") else -1
            assert api.set_algorithm_param.call_args.args[position] == expected
            subtype = key.split("_")[-2] if key.startswith("algorithm") else "0"
            for entry in optionals["AlgorithmInfo"]:
                if entry["SubType"] == subtype:
                    entry["Value"] = expected
        else:
            group, payload = api.set_dev_config_kv.call_args.args[-2:]
            optionals[group] = payload
        api.poll_devices = MagicMock(return_value=snapshot(data, STATUS={"optionals": optionals}))
        await data.async_refresh()
        assert entity.native_value == expected
    finally:
        await data.async_close()


@pytest.mark.parametrize("value", [True, 9.6, float("nan"), float("inf")])
async def test_effect_normalization_never_bypasses_sdk_input_validation(hass, value):
    api, data = configured(hass)
    try:
        with pytest.raises(HomeAssistantError):
            await numeric(data, "alarm_voice_volume").async_set_native_value(value)
        api.set_dev_config_kv.assert_not_called()
        assert data.settings._refresh_cancel is None
    finally:
        await data.async_close()


@pytest.mark.parametrize("mode", [0, True, "future"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("night_vision_duration", 90.6),
        ("night_vision_luminance", 75.6),
    ],
)
async def test_night_command_context_uses_sdk_mode_normalization(
    hass, mode, key, value
):
    api, data = configured(hass, mode=mode)
    entity = numeric(data, key)
    try:
        await entity.async_set_native_value(value)
        payload = api.set_dev_config_kv.call_args.args[-1]
        assert payload["graphicType"] == (1 if mode is True else 0)
        assert "duration" not in payload
        context = data.settings.command_data("CAM001", data.data["CAM001"])
        assert context["optionals"]["NightVision_Model"]["graphicType"] == (
            1 if mode is True else 0
        )
    finally:
        await data.async_close()


async def test_expired_sibling_projection_is_not_reused_for_next_write(hass, monkeypatch):
    api, data = configured(hass)
    monkeypatch.setattr(setting_writes, "COMMAND_CONTEXT_TTL", 0.01)
    try:
        await selector(data, "day_night_sensitivity").async_select_option(
            "day_night_sensitivity_high"
        )
        await asyncio.sleep(0.02)
        await selector(data, "day_night_mode").async_select_option("day_night_night")
        assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 2, "sensitivity": 1}
    finally:
        await data.async_close()


async def test_rejected_compound_write_has_no_secondary_accepted_effect(hass):
    api, data = configured(hass)
    try:
        await selector(data, "day_night_sensitivity").async_select_option(
            "day_night_sensitivity_high"
        )
        context = dict(data.settings._context)
        api.set_dev_config_kv.return_value = False
        with pytest.raises(HomeAssistantError):
            await selector(data, "day_night_mode").async_select_option("day_night_night")
        assert data.settings._context == context
        assert selector(data, "day_night_mode").current_option == "day_night_auto"
        assert (
            selector(data, "day_night_sensitivity").current_option == "day_night_sensitivity_low"
        )
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "key,group",
    [
        ("alarm_volume", "ChimeMusic"),
        ("alarm_voice_volume", "CustomVoice_Volume"),
        ("microphone_volume", "CustomVoice_Volume"),
    ],
)
@pytest.mark.parametrize("loss", ["group", "field"])
async def test_accepted_projection_does_not_resurrect_missing_audio_configuration(
    hass, key, group, loss
):
    api, data = configured(hass)
    entity = numeric(data, key)
    try:
        await entity.async_set_native_value(40)
        options = deepcopy(data.data["CAM001"]["STATUS"]["optionals"])
        if loss == "group":
            options.pop(group)
        else:
            options[group].pop("microphone_volume" if key == "microphone_volume" else "volume")
        api.poll_devices = MagicMock(return_value=snapshot(data, STATUS={"optionals": options}))
        await data.async_refresh()
        assert entity.native_value is None
        calls = api.set_dev_config_kv.call_count
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(45)
        assert api.set_dev_config_kv.call_count == calls
    finally:
        await data.async_close()


async def test_malformed_observed_enum_is_unknown_without_failing_poll(hass):
    _, api, data, *_ = controls(hass)
    data.data["CAM001"]["alarm_sound_mod"] = "SOFT"
    entity = selector(data, "alarm_sound_mod")
    api.alarm_sound = MagicMock(return_value=True)
    try:
        await entity.async_select_option("intensive")
        assert entity.current_option == "soft"
        api.poll_devices = MagicMock(
            return_value=snapshot(data, alarm_sound_mod="UNKNOWN_NEW_MODE")
        )
        await data.async_refresh()
        assert data.last_update_success
        assert entity.current_option is None
    finally:
        await data.async_close()
