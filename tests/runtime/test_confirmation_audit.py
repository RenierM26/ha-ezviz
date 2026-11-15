"""Regressions for independent review of effective cloud command state."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from unittest.mock import MagicMock

from pyezvizapi.constants import SupportExt
from pyezvizapi.devices.camera import EzvizCamera
from pyezvizapi.devices.observation import observed_fields
from pyezvizapi.devices.snapshot import CameraState, DeviceCapabilities
import pytest

from custom_components.ezviz_cloud import confirmation, number, select, switch
from homeassistant.exceptions import HomeAssistantError

from .test_setting_confirmation import controls, snapshot


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
async def test_day_night_writes_preserve_pending_sibling_and_forced_auto_mode(
    hass, encoded, reverse, layout
):
    api, data = configured(hass, encoded=encoded, layout=layout)
    mode, sensitivity = selector(data, "day_night_mode"), selector(data, "day_night_sensitivity")
    before = deepcopy(data.data)
    try:
        if reverse:
            await mode.async_select_option("day_night_night")
            await sensitivity.async_select_option("day_night_sensitivity_high")
            assert mode.current_option == "day_night_auto"  # Sensitivity really writes mode0.
            assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 0, "sensitivity": 3}
        else:
            await sensitivity.async_select_option("day_night_sensitivity_high")
            await mode.async_select_option("day_night_night")
            assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 2, "sensitivity": 3}
        assert sensitivity.current_option == "day_night_sensitivity_high"
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
async def test_audio_compound_writes_preserve_normalized_pending_siblings(
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
        assert voice.native_value == payload["volume"]
        assert mic.native_value == payload["microphone_volume"]
        assert data.data == before
    finally:
        await data.async_close()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("layout", ["STATUS", "optionals", "statusInfo"])
async def test_night_vision_writes_and_mode_aliases_preserve_pending_config(
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
        assert luminance.native_value == 76 and duration.native_value == 90
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
        assert entity.native_value == expected
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
        assert entity.native_value == expected and not data.settings.pending
    finally:
        await data.async_close()


@pytest.mark.parametrize("value", [True, 9.6, float("nan"), float("inf")])
async def test_effect_normalization_never_bypasses_sdk_input_validation(hass, value):
    api, data = configured(hass)
    try:
        with pytest.raises(HomeAssistantError):
            await numeric(data, "alarm_voice_volume").async_set_native_value(value)
        api.set_dev_config_kv.assert_not_called()
        assert not data.settings.pending
    finally:
        await data.async_close()


@pytest.mark.parametrize("kind", ["switch", "light", "motion"])
async def test_unknown_property_is_not_a_confirmed_off(hass, kind):
    entity, api, data, _, _, _, _, _, _ = controls(hass, kind)
    if kind == "switch":
        entity.entity_description = replace(
            entity.entity_description, value_fn=lambda d: (d.get("switches") or {}).get(1)
        )
    elif kind == "light":
        entity.entity_description = replace(
            entity.entity_description, is_on_value=lambda d: (d.get("switches") or {}).get(1)
        )
    try:
        await (
            entity.async_disable_motion_detection() if kind == "motion" else entity.async_turn_off()
        )
        unknown = snapshot(data, switches={}, alarm_notify=False)
        state = unknown.devices["CAM001"]
        values = state.as_dict()
        values.get("STATUS", {}).pop("globalStatus", None)
        unknown = replace(
            unknown,
            devices={
                **unknown.devices,
                "CAM001": replace(
                    state,
                    values=values,
                    capabilities=replace(state.capabilities, observed=frozenset()),
                ),
            },
        )
        api.poll_devices = MagicMock(return_value=unknown)
        await data.async_refresh()
        assert data.settings.pending and data.last_update_success
    finally:
        await data.async_close()


async def test_malformed_enum_confirmation_isolated_from_account_health(hass):
    _, api, data, *_ = controls(hass)
    data.data["CAM001"]["alarm_sound_mod"] = "SOFT"
    entity = selector(data, "alarm_sound_mod")
    api.alarm_sound = MagicMock(return_value=True)
    try:
        await entity.async_select_option("intensive")
        api.poll_devices = MagicMock(
            return_value=snapshot(data, alarm_sound_mod="UNKNOWN_NEW_MODE")
        )
        await data.async_refresh()
        assert data.last_update_success and data.settings.pending
        assert entity.current_option == "intensive"
    finally:
        await data.async_close()


@pytest.mark.parametrize("mode", [0, True, "future"])
@pytest.mark.parametrize(
    "key,value,field,expected",
    [
        ("night_vision_duration", 90.6, "duration", 60),
        ("night_vision_luminance", 75.6, "luminance", 76),
    ],
)
async def test_night_effects_use_exact_sdk_mode_normalization(
    hass, mode, key, value, field, expected
):
    api, data = configured(hass, mode=mode)
    entity = numeric(data, key)
    try:
        await entity.async_set_native_value(value)
        payload = api.set_dev_config_kv.call_args.args[-1]
        assert payload["graphicType"] == (1 if mode is True else 0)
        assert "duration" not in payload
        assert entity.native_value == expected
        for item in data.settings.pending.values():
            if item.project:
                context = data.settings.command_data("CAM001", data.data["CAM001"])
                assert context["optionals"]["NightVision_Model"]["graphicType"] == (
                    1 if mode is True else 0
                )
                break
    finally:
        await data.async_close()


async def test_expired_sibling_projection_is_not_reused_for_next_write(hass, monkeypatch):
    api, data = configured(hass)
    monkeypatch.setattr(confirmation, "PENDING_TIMEOUT", 0.01)
    try:
        await selector(data, "day_night_sensitivity").async_select_option(
            "day_night_sensitivity_high"
        )
        async with asyncio.timeout(2):
            while data.settings.pending:
                await asyncio.sleep(0.005)
        await selector(data, "day_night_mode").async_select_option("day_night_night")
        assert api.set_dev_config_kv.call_args.args[-1] == {"mode": 2, "sensitivity": 1}
    finally:
        await data.async_close()


async def test_rejected_compound_write_has_no_secondary_pending_effect(hass):
    api, data = configured(hass)
    try:
        await selector(data, "day_night_sensitivity").async_select_option(
            "day_night_sensitivity_high"
        )
        generation = data.settings.generation
        api.set_dev_config_kv.return_value = False
        with pytest.raises(HomeAssistantError):
            await selector(data, "day_night_mode").async_select_option("day_night_night")
        assert data.settings.generation == generation
        assert selector(data, "day_night_mode").current_option == "day_night_auto"
        assert (
            selector(data, "day_night_sensitivity").current_option == "day_night_sensitivity_high"
        )
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "key,target,section,field",
    [
        ("encrypted", False, "STATUS", "isEncrypt"),
        ("motion_detection", False, "STATUS", "globalStatus"),
        ("push_notify_alarm", True, "NODISTURB", "alarmEnable"),
        ("push_notify_call", True, "NODISTURB", "callingEnable"),
        ("offline_notify", False, "deviceInfos", "offlineNotify"),
    ],
)
@pytest.mark.parametrize("source", ["absent", "known", "null"])
async def test_real_sdk_defaulted_flag_requires_known_source(
    hass, key, target, section, field, source
):
    _, api, data, *_ = controls(hass)
    desc = next(item for item in switch.SWITCHES if item.key == key)
    entity = switch.EzvizSwitch(data, "CAM001", replace(desc, method=lambda *_: True))
    raw = {
        "deviceInfos": {
            "deviceSerial": "CAM001",
            "name": "Synthetic",
            "version": "1",
            "deviceCategory": "IPC",
            "deviceSubCategory": "Synthetic",
            "status": 1,
        },
        "STATUS": {},
        "WIFI": {},
        "SWITCH": {},
    }
    if source != "absent":
        raw.setdefault(section, {})[field] = 0 if source == "known" else None
    status = EzvizCamera(MagicMock(), "CAM001", raw).status(refresh=False)
    state = CameraState(
        "CAM001", "camera", status, DeviceCapabilities(observed_fields(raw, "camera"))
    )
    api.poll_devices = MagicMock(
        return_value=replace(
            data.inventory_snapshot, devices={**data.inventory_snapshot.devices, "CAM001": state}
        )
    )
    try:
        await (entity.async_turn_on() if target else entity.async_turn_off())
        await data.async_refresh()
        assert bool(data.settings.pending) is (source != "known")
        assert data.last_update_success
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "key,values",
    [
        ("OSD", {"optionals": {"OSD": []}}),
        (
            "intelligent_fill_light",
            {
                "FEATURE_INFO": {
                    "1": {
                        "Video": {
                            "SupplementLightMgr": {
                                "ImageSupplementLightModeSwitchParams": {"enabled": False}
                            }
                        }
                    }
                }
            },
        ),
        (
            "port_security_50161",
            {
                "NetworkSecurityProtection": {
                    "portSecurityList": [{"portNo": 50161, "enabled": False}]
                }
            },
        ),
        (
            "port_security_client_mode",
            {
                "NetworkSecurityProtection": {
                    "portSecurityList": [{"portNo": 443, "enabled": False}]
                }
            },
        ),
    ],
)
@pytest.mark.parametrize("known", [False, True])
async def test_defaulting_switch_helpers_require_explicit_configuration(hass, key, values, known):
    _, api, data, *_ = controls(hass)
    desc = next(item for item in switch.SWITCHES if item.key == key)
    entity = switch.EzvizSwitch(data, "CAM001", replace(desc, method=lambda *_: True))
    api.poll_devices = MagicMock(return_value=snapshot(data, **(values if known else {})))
    try:
        await entity.async_turn_off()
        await data.async_refresh()
        assert bool(data.settings.pending) is (not known)
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "key,values",
    [
        ("blc_region", {"optionals": {"inverse_mode": {"enable": 0, "position": 0}}}),
        ("image_style_setting", {"optionals": {"display_mode": {"mode": 1}}}),
        (
            "lens_defog",
            {
                "FEATURE_INFO": {
                    "1": {
                        "Video": {
                            "LensCleaning": {"DefogCfg": {"enabled": True, "defogMode": "auto"}}
                        }
                    }
                }
            },
        ),
        (
            "ptz_linked_tracking_range",
            {
                "FEATURE_INFO": {
                    "1": {
                        "Video": {
                            "PTZMasterSlaveTrace": {
                                "LinkedTrackingAdvancedCfg": {"trackingTakeoverEnabled": False}
                            }
                        }
                    }
                }
            },
        ),
    ],
)
@pytest.mark.parametrize("known", [False, True])
async def test_defaulting_select_helpers_require_explicit_configuration(hass, key, values, known):
    _, api, data, *_ = controls(hass)
    entity = selector(data, key)
    entity.entity_description = replace(
        entity.entity_description, set_current_option=lambda *_: True
    )
    api.poll_devices = MagicMock(return_value=snapshot(data, **(values if known else {})))
    try:
        await entity.async_select_option(entity.options[0])
        await data.async_refresh()
        assert bool(data.settings.pending) is (not known)
    finally:
        await data.async_close()


@pytest.mark.parametrize("on", [False, True])
async def test_unproven_intelligent_app_uses_bounded_expiry(hass, monkeypatch, on):
    _, api, data, *_ = controls(hass)
    monkeypatch.setattr(confirmation, "PENDING_TIMEOUT", 0.02)
    desc = next(item for item in switch.SWITCHES if item.key.startswith("intelligent_app_"))
    entity = switch.EzvizSwitch(data, "CAM001", replace(desc, method=lambda *_: True))
    api.poll_devices = MagicMock(return_value=snapshot(data))
    try:
        await (entity.async_turn_on() if on else entity.async_turn_off())
        await data.async_refresh()
        assert data.settings.pending
        async with asyncio.timeout(2):
            while data.settings.pending:
                await asyncio.sleep(0.005)
        assert entity.is_on is False
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "raw",
    [
        {"portSecurityList": [{"portNo": 80}]},
        {"portSecurityList": [{"portNo": 80, "enabled": None}]},
        {"portSecurityList": [{"portNo": 80, "enabled": "unknown"}]},
    ],
)
@pytest.mark.parametrize("on", [False, True])
async def test_lossy_sdk_port_default_cannot_confirm(hass, raw, on):
    _, api, data, *_ = controls(hass)
    desc = next(item for item in switch.SWITCHES if item.key == "port_security_client_mode")
    entity = switch.EzvizSwitch(data, "CAM001", replace(desc, method=lambda *_: True))
    api.poll_devices = MagicMock(return_value=snapshot(data, NetworkSecurityProtection=raw))
    try:
        await (entity.async_turn_on() if on else entity.async_turn_off())
        await data.async_refresh()
        assert data.settings.pending
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
async def test_pending_projection_does_not_resurrect_missing_audio_configuration(hass, key, group):
    api, data = configured(hass)
    entity = numeric(data, key)
    try:
        await entity.async_set_native_value(40)
        options = deepcopy(data.data["CAM001"]["STATUS"]["optionals"])
        options.pop(group)
        api.poll_devices = MagicMock(return_value=snapshot(data, STATUS={"optionals": options}))
        await data.async_refresh()
        assert entity.native_value is None
        calls = api.set_dev_config_kv.call_count
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(45)
        assert api.set_dev_config_kv.call_count == calls
    finally:
        await data.async_close()
