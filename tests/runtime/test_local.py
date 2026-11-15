"""Shared local credential selection, queued execution and camera-settings UI."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError
import pytest

import custom_components
from custom_components.ezviz_cloud import config_flow, local, options_flow
from custom_components.ezviz_cloud.const import DEFAULT_FETCH_MY_KEY, DOMAIN
from homeassistant import loader
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from . import test_local_rtsp as rtsp_tests
from .test_hardening import entities
from .test_local_rtsp import state

CAMERA = {"username": "admin", "password": "STICKER_SECRET", "enc_key": "ENCRYPTION_SECRET", "rtsp_uses_verification_code": False, "ephemeral_test_rtsp": False, "ffmpeg_arguments": "/Streaming/Channels/102"}
AUTH = local.CONF_LOCAL_CONTROL_AUTH
sdk = rtsp_tests.sdk


@pytest.mark.parametrize("mode,expected", [("verification_code", "STICKER_SECRET"), ("encryption_key", "ENCRYPTION_SECRET")])
@pytest.mark.parametrize("media_uses_sticker", [False, True])
def test_explicit_local_source_is_independent_of_media_selection(mode, expected, media_uses_sticker):
    config = local.LocalDeviceConfig.from_camera_options({**CAMERA, AUTH: mode, "rtsp_uses_verification_code": media_uses_sticker}, {"device_category": "IPC", "local_ip": "192.0.2.10"})
    assert config.password == expected
    assert config.host == "192.0.2.10"
    assert expected not in repr(config)


@pytest.mark.parametrize("mode,key", [("verification_code", "password"), ("encryption_key", "enc_key")])
@pytest.mark.parametrize("value", ["", None, DEFAULT_FETCH_MY_KEY])
def test_missing_selected_credential_never_falls_back(mode, key, value):
    with pytest.raises(ValueError):
        local.LocalDeviceConfig.from_camera_options({**CAMERA, AUTH: mode, key: value}, {"device_category": "IPC", "local_ip": "192.0.2.10"})


@pytest.mark.parametrize("mode,category", [("disabled", "IPC"), ("verification_code", "BatteryCamera"), ("encryption_key", "BDoorBell"), ("future", "IPC")])
def test_disabled_unknown_and_battery_profiles_cannot_construct_client(mode, category):
    with pytest.raises((ValueError, PyEzvizError)):
        local.LocalDeviceConfig.from_camera_options({**CAMERA, AUTH: mode}, {"device_category": category, "local_ip": "192.0.2.10"})


async def test_shared_executor_retains_lock_after_cancellation_and_resolves_latest_profile(hass):
    entry, _, account, *_ = entities(hass)
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, AUTH: "verification_code"}}})
    device = account.local_device("CAM001")
    assert account.local_device("CAM001") is device
    assert account.local_device("OTHER") is not device
    entered, release = Event(), Event()
    captured = []

    def first_operation(config, serial):
        captured.append((config.host, config.password, serial))
        entered.set()
        assert release.wait(5)
        return 1

    second_operation = MagicMock(side_effect=lambda config, serial: (config.host, config.password, serial))
    first = asyncio.create_task(device.async_execute(first_operation))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(account.local_device("CAM001").async_execute(second_operation))
        await asyncio.sleep(0)
        second_operation.assert_not_called()
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, AUTH: "encryption_key"}}})
        account.data["CAM001"]["local_ip"] = "192.0.2.20"
        account.credentials.commit("CAM001", "NEW_ENCRYPTION_SECRET")
        release.set()
        assert await second == ("192.0.2.20", "NEW_ENCRYPTION_SECRET", "CAM001")
        assert captured == [("192.0.2.1", "STICKER_SECRET", "CAM001")]
    finally:
        release.set()
        if second:
            await second
        await account.async_close()


async def test_local_profile_without_entry_is_not_executable(hass):
    _, _, account, *_ = entities(hass)
    try:
        account.config_entry = None
        with pytest.raises(PyEzvizError):
            account.local_device("CAM001").configuration()
    finally:
        await account.async_close()


@pytest.fixture
def flow_setup(hass, monkeypatch):
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = config_flow
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    entry, api, account, *_ = entities(hass)
    api.get_2fa_check_code = MagicMock()
    entry.runtime_data = SimpleNamespace(coordinator=account)
    return entry, api, account, reload


async def edit(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert result["menu_options"] == ["cloud", "camera_select"]
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "camera_select"})
    return await manager.async_configure(result["flow_id"], {"serial": "CAM001"})


@pytest.mark.parametrize("mode,expected", [("disabled", None), ("verification_code", CAMERA["password"]), ("encryption_key", CAMERA["enc_key"])])
@pytest.mark.parametrize("media_uses_sticker", [False, True])
async def test_camera_dropdown_reads_selected_existing_key_only(hass, sdk, flow_setup, mode, expected, media_uses_sticker):
    client, factory = sdk
    entry, _, account, _ = flow_setup
    original = {"timeout": 33, "cameras": {"CAM001": {"future": 7}, "OTHER": {"future": 9}}}
    hass.config_entries.async_update_entry(entry, options=original)
    try:
        result = await edit(hass, entry)
        dropdown = next(value for key, value in result["data_schema"].schema.items() if key.schema == AUTH)
        assert dropdown.config["mode"] == "dropdown"
        assert dropdown.config["options"] == ["disabled", "verification_code", "encryption_key"]
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "disabled"
        assert defaults["ephemeral_test_rtsp"] is False
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, AUTH: mode, "rtsp_uses_verification_code": media_uses_sticker})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        stored = entry.options["cameras"]["CAM001"]
        assert stored["password"] == CAMERA["password"] and stored["enc_key"] == CAMERA["enc_key"]
        assert stored.get(AUTH, "disabled") == mode
        assert "local_rtsp" not in stored and "host" not in stored
        assert stored["rtsp_uses_verification_code"] is media_uses_sticker
        assert stored["future"] == 7 and entry.options["cameras"]["OTHER"] == {"future": 9}
        assert entry.options["timeout"] == 33
        assert factory.call_count == int(expected is not None)
        if expected is not None:
            assert factory.call_args.args[1] == expected
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_camera_disabled_profile_does_not_probe_or_create_an_extra_secret(hass, sdk, flow_setup):
    client, factory = sdk
    entry, _, account, _ = flow_setup
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, AUTH: "verification_code", "local_rtsp": {"password": "OBSOLETE_LAN_SECRET"}}}})
    try:
        result = await edit(hass, entry)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, AUTH: "disabled"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"][AUTH] == "disabled"
        assert entry.options["cameras"]["CAM001"]["local_rtsp"] == {}
        factory.assert_not_called()
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_battery_camera_dropdown_offers_disabled_only(hass, sdk, flow_setup):
    _, factory = sdk
    entry, _, account, _ = flow_setup
    account.data["CAM001"]["device_category"] = "BatteryCamera"
    try:
        result = await edit(hass, entry)
        dropdown = next(value for key, value in result["data_schema"].schema.items() if key.schema == AUTH)
        assert dropdown.config["options"] == ["disabled"]
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, AUTH: "verification_code"})
        factory.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure,error", [("invalid", "local_control_invalid"), ("unknown", "local_control_unsupported"), ("network", "local_control_cannot_connect"), ("sdk", "local_control_cannot_connect")])
async def test_local_validation_error_retains_camera_fields_without_commit(hass, sdk, flow_setup, failure, error):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    payload = {**CAMERA, AUTH: "verification_code"}
    if failure == "invalid":
        payload["password"] = ""
    elif failure == "unknown":
        client.services_switch_state.return_value = state(None)
    else:
        client.services_switch_state.side_effect = OSError("SECRET") if failure == "network" else PyEzvizError("SECRET")
    before = entry.options
    try:
        result = await edit(hass, entry)
        result = await hass.config_entries.options.async_configure(result["flow_id"], payload)
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "camera_edit"
        assert result["errors"] == {AUTH: error}
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "verification_code" and defaults["enc_key"] == CAMERA["enc_key"]
        assert entry.options == before
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("local_failure", [False, True])
@pytest.mark.parametrize("mode,field,method,code_field", [("encryption_key", "enc_key", "get_cam_key", "cam_encryption_2fa_code"), ("verification_code", "password", "get_cam_auth_code", "cam_verification_2fa_code")])
async def test_mfa_resolution_is_used_for_local_auth_and_errors_return_to_camera_form(hass, sdk, flow_setup, local_failure, mode, field, method, code_field):
    client, factory = sdk
    entry, api, account, _ = flow_setup
    setattr(api, method, MagicMock(side_effect=[EzvizAuthVerificationCode(), "FETCHED_LOCAL_SECRET"]))
    if local_failure:
        client.services_switch_state.side_effect = OSError("synthetic")
    try:
        result = await edit(hass, entry)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, field: DEFAULT_FETCH_MY_KEY, AUTH: mode})
        assert result["step_id"] == "camera_edit_2fa"
        factory.assert_not_called()
        result = await hass.config_entries.options.async_configure(result["flow_id"], {code_field: "111222"})
        assert factory.call_args.args[1] == "FETCHED_LOCAL_SECRET"
        if local_failure:
            assert result["step_id"] == "camera_edit" and result["errors"] == {AUTH: "local_control_cannot_connect"}
            defaults = result["data_schema"]({})
            assert defaults[field] == "FETCHED_LOCAL_SECRET"
            assert defaults[AUTH] == mode and defaults["ephemeral_test_rtsp"] is False
        else:
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert entry.options["cameras"]["CAM001"][field] == "FETCHED_LOCAL_SECRET"
            assert entry.options["cameras"]["CAM001"][AUTH] == mode
            assert code_field not in entry.options["cameras"]["CAM001"]
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_concurrent_camera_credential_edit_reopens_current_profile(hass, sdk, flow_setup, monkeypatch):
    client, factory = sdk
    entry, _, account, _ = flow_setup
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, AUTH: "verification_code"}}})
    original = options_flow.async_validate_local_controls
    changed = {**CAMERA, "password": "NEW_STICKER", "enc_key": "NEW_ENC", AUTH: "encryption_key", "future": 12}

    async def conflict(*args):
        await original(*args)
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": changed}})

    monkeypatch.setattr(options_flow, "async_validate_local_controls", conflict)
    try:
        result = await edit(hass, entry)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, AUTH: "verification_code"})
        assert result["errors"] == {"base": "options_changed"}
        assert factory.call_args.args[1] == CAMERA["password"]
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "encryption_key" and defaults["enc_key"] == "NEW_ENC"
        monkeypatch.setattr(options_flow, "async_validate_local_controls", original)
        result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert factory.call_args.args[1] == "NEW_ENC"
        assert entry.options["cameras"]["CAM001"]["future"] == 12
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()
