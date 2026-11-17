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

from custom_components.ezviz_cloud import local, local_rtsp, switch
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import LocalRtspCoordinator
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from .test_hardening import entities

CONFIG = {"host": "192.0.2.10", "username": "admin", "password": "LAN_SECRET_MARKER"}
RAW = {"servicesSwitch": {"hiksdk": 1, "web": 0, "rtsp": 0, "upnp": 0, "future": 17}, "future": [1]}


def state(value):
    raw = deepcopy(RAW)
    raw["servicesSwitch"]["rtsp"] = value
    return ezviz_lan_services_switch_state(raw)


@pytest.fixture
def sdk(monkeypatch):
    clock = MagicMock(return_value=0.0)
    def advance(seconds):
        clock.return_value += seconds
    monkeypatch.setattr(local_rtsp, "monotonic", clock, raising=False)
    monkeypatch.setattr(local_rtsp, "sleep", MagicMock(side_effect=advance))
    client = MagicMock()
    client.services_switch_state.return_value = state(0)
    client.services_switch_set.return_value = SimpleNamespace(
        succeeded=True, json=lambda: {"statusCode": 1}
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(local, "HcNetSdkPurePythonClient", factory)
    return client, factory


@pytest.mark.parametrize("value,expected", [(0, False), (1, True), ("0", False), ("1", True)])
def test_status_uses_explicit_known_raw_value(sdk, value, expected):
    client, factory = sdk
    client.services_switch_state.return_value = state(value)
    config = local.LocalDeviceConfig.from_options(CONFIG)
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
        local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_not_called()


@pytest.mark.parametrize("raw", [None, {}, {"servicesSwitch": []}])
def test_missing_service_is_unsupported(sdk, raw):
    client, _ = sdk
    client.services_switch_state.return_value = EzvizLanServicesSwitchState(raw=raw)
    with pytest.raises(local_rtsp.LocalRtspUnsupported):
        local_rtsp.read_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001")


@pytest.mark.parametrize("on", [False, True])
def test_change_preserves_payload_and_reads_back_once(sdk, on):
    client, _ = sdk
    before = state(int(not on))
    original = deepcopy(before.raw)
    client.services_switch_state.side_effect = [before, state(int(on))]
    assert local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", on) is on
    client.services_switch_set.assert_called_once_with(before.raw, rtsp=int(on))
    assert before.raw == original
    assert client.services_switch_state.call_count == 2


@pytest.mark.parametrize("on", [False, True])
def test_idempotent_write_only_reads(sdk, on):
    client, _ = sdk
    client.services_switch_state.return_value = state(int(on))
    assert local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", on) is on
    client.services_switch_state.assert_called_once()
    client.services_switch_set.assert_not_called()


@pytest.mark.parametrize("status", [0, True, "1", None])
def test_application_denial_is_not_transport_success(sdk, status):
    client, _ = sdk
    client.services_switch_set.return_value = SimpleNamespace(
        succeeded=True, json=lambda: {"statusCode": status}
    )
    with pytest.raises(PyEzvizError):
        local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_called_once()
    assert client.services_switch_state.call_count == 16  # Initial read plus the recovery window.
    assert local_rtsp.monotonic.return_value == 75


@pytest.mark.parametrize("failure", ["transport", "write", "readback", "mismatch", "unknown"])
def test_unknown_outcome_never_retries_write(sdk, failure):
    client, _ = sdk
    if failure == "transport":
        client.services_switch_set.return_value = SimpleNamespace(succeeded=False)
    elif failure == "write":
        client.services_switch_set.side_effect = OSError("synthetic")
    elif failure == "readback":
        client.services_switch_state.side_effect = [state(0), *[OSError("synthetic")] * 16]
    elif failure == "unknown":
        client.services_switch_state.side_effect = [state(0), *[state(None)] * 16]
    with pytest.raises((PyEzvizError, OSError)):
        local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", True)
    client.services_switch_set.assert_called_once()


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("write_result", ["ack", "lost", "denied", "malformed"])
@pytest.mark.parametrize("readback", ["immediate", "stale", "restart", "unknown"])
def test_final_observation_confirms_change_even_when_ack_is_lost(sdk, on, write_result, readback):
    client, _ = sdk
    before = state(int(not on))
    observed = state(int(on))
    transient = {"stale": before, "restart": OSError("restarting"), "unknown": state(None)}
    client.services_switch_state.side_effect = [before, *([transient[readback]] if readback != "immediate" else []), observed]
    if write_result == "lost":
        client.services_switch_set.side_effect = PyEzvizError("ACK lost")
    elif write_result == "denied":
        client.services_switch_set.return_value = SimpleNamespace(succeeded=False)
    elif write_result == "malformed":
        client.services_switch_set.return_value = SimpleNamespace(succeeded=True, json=lambda: {"statusCode": "unexpected"})
    assert local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", on) is on
    client.services_switch_set.assert_called_once_with(before.raw, rtsp=int(on))
    assert client.services_switch_state.call_count == (2 if readback == "immediate" else 3)
    assert local_rtsp.sleep.call_args_list == ([] if readback == "immediate" else [((5.0,), {})])


async def test_disable_service_restart_returns_success_and_publishes_observed_off(hass, sdk):
    client, _ = sdk
    _, _, account, coord, entity = coordinator(hass)
    try:
        client.services_switch_state.return_value = state(1)
        await coord.async_refresh()
        client.services_switch_state.side_effect = [state(1), OSError("service restarting"), state(1), state(0)]
        client.services_switch_set.side_effect = OSError("ACK lost")
        await entity.async_turn_off()
        assert entity.is_on is False and entity.available
        client.services_switch_set.assert_called_once()
        assert client.services_switch_state.call_count == 5
    finally:
        await coord.async_shutdown()
        await account.async_close()


@pytest.mark.parametrize("on", [False, True])
def test_husky_recovery_over_fifty_seconds_verifies_without_repeating_write(sdk, on):
    client, _ = sdk
    before = state(int(not on))
    writes = []
    client.services_switch_set.side_effect = lambda *a, **kw: writes.append(kw)

    def read():
        if not writes:
            return before
        elapsed = local_rtsp.monotonic.return_value
        if elapsed < 7:
            raise PyEzvizError("HCNetSDK unavailable during restart")
        if elapsed < 52:
            local_rtsp.monotonic.return_value += 5  # Recorded socket timeouts.
            raise TimeoutError("camera restarting")
        return state(int(on))

    client.services_switch_state.side_effect = read
    assert local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", on) is on
    assert 52 <= local_rtsp.monotonic.return_value <= 75
    client.services_switch_set.assert_called_once_with(before.raw, rtsp=int(on))


@pytest.mark.parametrize("observed", [0, 1])
def test_slow_last_read_finishes_without_initiating_another_after_deadline(sdk, observed):
    client, _ = sdk
    reads = 0
    def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return state(0)
        local_rtsp.monotonic.return_value = 80  # One already-started SDK operation.
        return state(observed)
    client.services_switch_state.side_effect = read
    if observed:
        assert local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", True) is True
    else:
        with pytest.raises(PyEzvizError):
            local_rtsp.write_local_rtsp(local.LocalDeviceConfig(**CONFIG), "CAM001", True)
    assert reads == 2
    local_rtsp.sleep.assert_not_called()
    client.services_switch_set.assert_called_once()


@pytest.mark.parametrize("patch", [
    {"host": "localhost"}, {"host": "::1"}, {"host": "127.0.0.1"},
    {"host": "224.0.0.1"}, {"host": "0.0.0.0"}, {"host": "255.255.255.255"},
    {"host": 123}, {"username": ""}, {"password": ""}, {"password": "a\0b"}, {"password": "a" * 1025},
])
def test_invalid_configuration_fails_before_client_creation(sdk, patch):
    _, factory = sdk
    with pytest.raises((ValueError, TypeError)):
        local.LocalDeviceConfig.from_options({**CONFIG, **patch})
    factory.assert_not_called()


@pytest.mark.parametrize("category,eligible", [
    ("IPC", True), ("BatteryCamera", False), ("BatteryCamera_AlwaysOn", False),
    ("BDoorBell", False), ("CatEye", False), ("XVR", False), (None, False),
])
def test_only_nonbattery_camera_family_is_eligible(category, eligible):
    assert local.local_controls_eligible({"device_category": category}) is eligible


def coordinator(hass):
    entry, api, account, *_ = entities(hass)
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_control_auth": "verification_code", "username": "admin", "password": CONFIG["password"], "enc_key": "OTHER_KEY"}}})
    rtsp = LocalRtspCoordinator(account, "CAM001")
    return entry, api, account, rtsp, switch.EzvizRtspSwitch(rtsp)


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
        assert entity.device_info == DeviceInfo(
            identifiers={(DOMAIN, "CAM001")},
            manufacturer="EZVIZ",
            model="CP8",
            name="Synthetic",
            sw_version="1",
            serial_number="CAM001",
        )
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


async def test_flow_only_state_owner_is_closed_with_the_account(hass):
    _, _, account, *_ = entities(hass)
    rtsp = account.local_rtsp_coordinator("CAM001")
    assert account.local_rtsp_coordinator("CAM001") is rtsp
    assert not rtsp._shutdown_requested
    await account.async_close()
    assert rtsp._shutdown_requested and rtsp._unsub_refresh is None
    with pytest.raises(HomeAssistantError):
        account.local_rtsp_coordinator("CAM002")


@pytest.mark.parametrize("failed", [False, True])
async def test_completed_result_delivery_during_close_cannot_restart_polling(hass, sdk, monkeypatch, failed):
    client, _ = sdk
    entry, _, account, *_ = entities(hass)
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"local_control_auth": "verification_code", "username": "admin", "password": CONFIG["password"]}}})
    rtsp = account.local_rtsp_coordinator("CAM001")
    await rtsp.async_refresh()
    notified = MagicMock()
    remove_listener = rtsp.async_add_listener(notified)
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = rtsp.device.async_execute
    async def gated_result(*args, **kwargs):
        value, error = None, None
        try:
            value = await original(*args, **kwargs)
        except PyEzvizError as err:
            error = err
        entered.set()
        await release.wait()
        if error is not None:
            raise error
        return value
    shutdown = rtsp.async_shutdown
    async def signal_shutdown():
        await shutdown()
        stopped.set()
    monkeypatch.setattr(rtsp.device, "async_execute", gated_result)
    monkeypatch.setattr(rtsp, "async_shutdown", signal_shutdown)
    client.services_switch_state.side_effect = PyEzvizError("synthetic") if failed else [state(0), state(1)]
    setter = asyncio.create_task(rtsp.async_set_enabled(True))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(account.async_close())
        await asyncio.wait_for(stopped.wait(), 2)
        assert rtsp._unsub_refresh is None and not closing.done()
        release.set()
        if failed:
            with pytest.raises(HomeAssistantError):
                await setter
        else:
            await setter
        await closing
        notified.assert_not_called()
        assert rtsp.data is False and rtsp._unsub_refresh is None
    finally:
        release.set()
        await asyncio.gather(setter, return_exceptions=True)
        remove_listener()
        if closing:
            await closing
        await account.async_close()
