"""Local-only RTSP configuration, real HA coordinators and synthetic SDK IO."""

import asyncio
from copy import deepcopy
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.local.hcnetsdk.api import (
    EzvizLanServicesSwitchState,
    ezviz_lan_services_switch_set_payload,
    ezviz_lan_services_switch_state,
)
import pytest

import custom_components
from custom_components.ezviz_cloud import config_flow, local_rtsp, switch
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import LocalRtspCoordinator
from homeassistant import loader
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from .test_hardening import entities

CONFIG = {"host": "192.0.2.10", "username": "admin", "password": "LAN_SECRET_MARKER"}
RAW = {"servicesSwitch": {"hiksdk": 1, "web": 0, "rtsp": 0, "upnp": 0, "future": 17}, "future": [1]}


def state(value):
    raw = deepcopy(RAW)
    raw["servicesSwitch"]["rtsp"] = value
    return ezviz_lan_services_switch_state(raw)


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.services_switch_state.return_value = state(0)
    client.services_switch_set.return_value = SimpleNamespace(
        succeeded=True, json=lambda: {"statusCode": 1}
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(local_rtsp, "HcNetSdkPurePythonClient", factory)
    return client, factory


@pytest.mark.parametrize("value,expected", [(0, False), (1, True), ("0", False), ("1", True)])
def test_status_uses_explicit_known_raw_value(sdk, value, expected):
    client, factory = sdk
    client.services_switch_state.return_value = state(value)
    config = local_rtsp.LocalRtspConfig.from_options(CONFIG)
    assert local_rtsp.read_local_rtsp(config, "CAM001") is expected
    args, kwargs = factory.call_args
    assert args[0].command_port == 8000 and args[0].host == CONFIG["host"]
    assert args[1] == CONFIG["password"] and kwargs["timeout"] == 5
    assert CONFIG["password"] not in repr(config)
    client.services_switch_set.assert_not_called()


@pytest.mark.parametrize("value", [None, True, False, 2, "true", "", 0.0, {}, []])
def test_unknown_switch_never_becomes_false_or_allows_a_write(sdk, value):
    client, _ = sdk
    client.services_switch_state.return_value = state(value)
    with pytest.raises(local_rtsp.LocalRtspUnsupported):
        local_rtsp.write_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_not_called()


@pytest.mark.parametrize("raw", [None, {}, {"servicesSwitch": []}])
def test_missing_service_is_unsupported(sdk, raw):
    client, _ = sdk
    client.services_switch_state.return_value = EzvizLanServicesSwitchState(raw=raw)
    with pytest.raises(local_rtsp.LocalRtspUnsupported):
        local_rtsp.read_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001")


@pytest.mark.parametrize("on", [False, True])
def test_change_preserves_payload_and_reads_back_once(sdk, on):
    client, _ = sdk
    before = state(int(not on))
    original = deepcopy(before.raw)
    client.services_switch_state.side_effect = [before, state(int(on))]
    assert local_rtsp.write_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001", on) is on
    client.services_switch_set.assert_called_once_with(before.raw, rtsp=int(on))
    assert before.raw == original
    assert client.services_switch_state.call_count == 2


@pytest.mark.parametrize("on", [False, True])
def test_idempotent_write_only_reads(sdk, on):
    client, _ = sdk
    client.services_switch_state.return_value = state(int(on))
    assert local_rtsp.write_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001", on) is on
    client.services_switch_state.assert_called_once()
    client.services_switch_set.assert_not_called()


@pytest.mark.parametrize("status", [0, True, "1", None])
def test_application_denial_is_not_transport_success(sdk, status):
    client, _ = sdk
    client.services_switch_set.return_value = SimpleNamespace(
        succeeded=True, json=lambda: {"statusCode": status}
    )
    with pytest.raises(PyEzvizError):
        local_rtsp.write_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_called_once()
    client.services_switch_state.assert_called_once()  # No write retry.


@pytest.mark.parametrize("failure", ["transport", "write", "readback", "mismatch"])
def test_unknown_outcome_never_retries_write(sdk, failure):
    client, _ = sdk
    if failure == "transport":
        client.services_switch_set.return_value = SimpleNamespace(succeeded=False)
    elif failure == "write":
        client.services_switch_set.side_effect = OSError("synthetic")
    elif failure == "readback":
        client.services_switch_state.side_effect = [state(0), OSError("synthetic")]
    with pytest.raises((PyEzvizError, OSError)):
        local_rtsp.write_local_rtsp(local_rtsp.LocalRtspConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_called_once()


@pytest.mark.parametrize("patch", [
    {"host": "localhost"}, {"host": "::1"}, {"host": "127.0.0.1"},
    {"host": "224.0.0.1"}, {"host": "0.0.0.0"}, {"host": "255.255.255.255"},
    {"host": 123}, {"username": ""}, {"password": ""}, {"password": "a\0b"}, {"password": "a" * 1025},
])
def test_invalid_configuration_fails_before_client_creation(sdk, patch):
    _, factory = sdk
    with pytest.raises((ValueError, TypeError)):
        local_rtsp.LocalRtspConfig.from_options({**CONFIG, **patch})
    factory.assert_not_called()


@pytest.mark.parametrize("category,eligible", [
    ("IPC", True), ("BatteryCamera", False), ("BatteryCamera_AlwaysOn", False),
    ("BDoorBell", False), ("CatEye", False), ("XVR", False), (None, False),
])
def test_only_nonbattery_camera_family_is_eligible(category, eligible):
    assert local_rtsp.local_rtsp_eligible({"device_category": category}) is eligible


def coordinator(hass):
    entry, api, account, *_ = entities(hass)
    local = LocalRtspCoordinator(account, "CAM001", local_rtsp.LocalRtspConfig(**CONFIG))
    return entry, api, account, local, switch.EzvizRtspSwitch(local)


async def test_local_read_write_is_offloop_observed_and_cloud_independent(hass, sdk):
    client, _ = sdk
    _entry, _, account, local, entity = coordinator(hass)
    threads = []
    client.services_switch_state.side_effect = lambda: (threads.append(get_ident()), state(1))[1]
    account.async_request_refresh = AsyncMock()
    try:
        account.last_update_success = False
        await local.async_refresh()
        assert entity.available and entity.is_on is True
        assert entity.unique_id == "CAM001_local_rtsp"
        assert entity.device_info["identifiers"] == {(DOMAIN, "CAM001")}
        assert entity.entity_category.value == "config"
        assert threads and all(item != get_ident() for item in threads)
        account.async_request_refresh.assert_not_awaited()
        client.services_switch_state.side_effect = [state(1), state(0)]
        await entity.async_turn_off()
        assert entity.is_on is False and entity.available
        client.services_switch_state.side_effect = [state(0), state(1)]
        await entity.async_turn_on()
        assert entity.is_on is True
    finally:
        await local.async_shutdown()
        await account.async_close()


async def test_local_failure_is_unavailable_not_off_and_recovers(hass, sdk):
    client, _ = sdk
    _, _, account, local, entity = coordinator(hass)
    try:
        client.services_switch_state.return_value = state(1)
        await local.async_refresh()
        client.services_switch_state.side_effect = OSError("LAN_SECRET_MARKER")
        await local.async_refresh()
        assert not entity.available and entity.is_on is True
        assert "LAN_SECRET_MARKER" not in str(local.last_exception)
        assert account.last_update_success
        client.services_switch_state.side_effect = None
        client.services_switch_state.return_value = state(0)
        await local.async_refresh()
        assert entity.available and entity.is_on is False
        client.services_switch_set.side_effect = PyEzvizError("LAN_SECRET_MARKER")
        with pytest.raises(HomeAssistantError) as error:
            await entity.async_turn_on()
        assert not entity.available and entity.is_on is False
        assert "LAN_SECRET_MARKER" not in str(error.value)
    finally:
        await local.async_shutdown()
        await account.async_close()


async def test_cancelled_local_write_remains_owned_and_unload_waits(hass, sdk):
    client, _ = sdk
    _, api, account, local, entity = coordinator(hass)
    entered, release = Event(), Event()

    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(succeeded=True, json=lambda: {"statusCode": 1})

    client.services_switch_state.side_effect = [state(0), state(1)]
    client.services_switch_set.side_effect = blocked
    task = asyncio.create_task(entity.async_turn_on())
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await local.async_shutdown()
        close = asyncio.create_task(account.async_close())
        await asyncio.sleep(0)
        assert not close.done()
        api.close_session.assert_not_called()
        release.set()
        await close
        api.close_session.assert_called_once()
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()
    finally:
        release.set()
        if close:
            await close
        await local.async_shutdown()
        await account.async_close()


@pytest.fixture
def flow_setup(hass, monkeypatch):
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = config_flow
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    entry, _, account, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=account)
    return entry, account


async def start_flow(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    return await manager.async_configure(result["flow_id"], {"next_step_id": "local_rtsp_select"})


async def select_camera(hass, entry):
    result = await start_flow(hass, entry)
    return await hass.config_entries.options.async_configure(result["flow_id"], {"serial": "CAM001"})


async def test_optin_reads_only_and_preserves_unrelated_camera_options(hass, sdk, flow_setup):
    client, _ = sdk
    entry, account = flow_setup
    original = {"timeout": 33, "cameras": {"CAM001": {"password": "RTSP_NOT_LAN", "other": 7}, "OTHER": {"future": 1}}}
    hass.config_entries.async_update_entry(entry, options=original)
    try:
        result = await select_camera(hass, entry)
        assert result["step_id"] == "local_rtsp"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"enabled": True, **CONFIG}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"] == {**original["cameras"]["CAM001"], "local_rtsp": CONFIG}
        assert entry.options["cameras"]["OTHER"] == original["cameras"]["OTHER"]
        assert entry.options["timeout"] == 33
        client.services_switch_set.assert_not_called()
        client.services_switch_state.assert_called_once()
    finally:
        await account.async_close()


@pytest.mark.parametrize("enable", [False, True])
async def test_edit_retains_masked_password_or_optout_clears_it_without_io(hass, sdk, flow_setup, enable):
    client, _ = sdk
    entry, account = flow_setup
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_rtsp": CONFIG}}})
    try:
        result = await select_camera(hass, entry)
        assert "LAN_SECRET_MARKER" not in repr(result)
        assert "LAN_SECRET_MARKER" not in repr(result["data_schema"].schema)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enabled": enable, **CONFIG, "password": ""})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"]["local_rtsp"] == (CONFIG if enable else {})
        assert client.services_switch_state.call_count == int(enable)
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("kind,error", [("invalid", "local_rtsp_invalid"), ("unknown", "local_rtsp_unsupported"), ("network", "local_rtsp_cannot_connect"), ("sdk", "local_rtsp_cannot_connect"), ("conflict", "options_changed")])
async def test_flow_errors_do_not_commit_or_expose_credentials(hass, sdk, flow_setup, kind, error):
    client, _ = sdk
    entry, account = flow_setup
    try:
        result = await select_camera(hass, entry)
        payload = {"enabled": True, **CONFIG}
        if kind == "invalid":
            payload["host"] = "localhost"
        elif kind == "unknown":
            client.services_switch_state.return_value = state(None)
        elif kind == "network":
            client.services_switch_state.side_effect = OSError("LAN_SECRET_MARKER")
        elif kind == "sdk":
            client.services_switch_state.side_effect = PyEzvizError("LAN_SECRET_MARKER")
        else:
            hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_rtsp": {**CONFIG, "host": "192.0.2.11"}}}})
        before = entry.options
        result = await hass.config_entries.options.async_configure(result["flow_id"], payload)
        assert result["errors"] == {"base": error}
        assert entry.options == before
        assert "LAN_SECRET_MARKER" not in repr(result["data_schema"].schema)
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("stage", ["select", "edit"])
@pytest.mark.parametrize("failure", ["runtime", "shutdown", "battery", "missing"])
async def test_flow_ineligible_or_unavailable_aborts(hass, sdk, flow_setup, stage, failure):
    entry, account = flow_setup
    try:
        result = await (select_camera(hass, entry) if stage == "edit" else hass.config_entries.options.async_init(entry.entry_id))
        if failure == "runtime":
            del entry.runtime_data
        elif failure == "shutdown":
            account.begin_shutdown()
        elif failure == "battery":
            account.data["CAM001"]["device_category"] = "BatteryCamera"
        else:
            account.data = {}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"enabled": False, **CONFIG} if stage == "edit" else {"next_step_id": "local_rtsp_select"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == ("integration_unavailable" if failure in ("runtime", "shutdown") else "no_local_rtsp_cameras")
    finally:
        await account.async_close()


async def test_cancelled_waiter_still_publishes_verified_readback(hass, sdk):
    client, _ = sdk
    _, _, account, local, entity = coordinator(hass)
    entered, release = Event(), Event()

    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(succeeded=True, json=lambda: {"statusCode": 1})

    client.services_switch_state.side_effect = [state(0), state(1)]
    client.services_switch_set.side_effect = blocked
    task = asyncio.create_task(entity.async_turn_on())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        async with asyncio.timeout(2):
            while account._action_futures:
                await asyncio.sleep(0.005)
        assert entity.is_on is True and entity.available
    finally:
        release.set()
        await local.async_shutdown()
        await account.async_close()


def test_real_sdk_patch_preserves_unknown_and_other_service_fields():
    before = deepcopy(RAW)
    payload = ezviz_lan_services_switch_set_payload(before, rtsp=1)
    assert payload == {**RAW, "servicesSwitch": {**RAW["servicesSwitch"], "rtsp": 1}}
    assert before == RAW


async def test_malformed_saved_optional_lan_config_can_be_reconfigured(hass, sdk, flow_setup):
    client, _ = sdk
    entry, account = flow_setup
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_rtsp": True, "future": 9}}})
    try:
        result = await select_camera(hass, entry)
        assert result["step_id"] == "local_rtsp"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enabled": True, **CONFIG})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"] == {"local_rtsp": CONFIG, "future": 9}
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_conflict_redisplay_keeps_current_host_and_saved_password_together(hass, sdk, flow_setup):
    client, factory = sdk
    entry, account = flow_setup
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_rtsp": CONFIG}}})
    try:
        result = await select_camera(hass, entry)
        changed = {**CONFIG, "host": "192.0.2.20", "username": "new-user", "password": "NEW_LAN_SECRET"}
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_rtsp": changed}}})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enabled": True, **CONFIG, "password": ""})
        assert result["errors"] == {"base": "options_changed"}
        defaults = result["data_schema"]({})
        assert defaults == {"enabled": True, **changed, "password": ""}
        assert "NEW_LAN_SECRET" not in repr(result["data_schema"].schema)
        assert factory.call_args.args[0].host == CONFIG["host"]
        assert factory.call_args.args[1] == CONFIG["password"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert factory.call_args.args[0].host == changed["host"]
        assert factory.call_args.args[1] == changed["password"]
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()
