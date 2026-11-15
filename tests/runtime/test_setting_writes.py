"""Cloud propagation delay with real coordinator polling and observed-only settings."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi import DeviceSnapshot
from pyezvizapi.devices.snapshot import CameraState, DeviceHealth, LightState, PlugState
import pytest

from custom_components.ezviz_cloud import light, number, select, setting_writes, switch, text
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from .test_hardening import entities


def snapshot(data, **changes):
    """Produce a detached network observation, never an SDK optimistic cache."""
    now = datetime.now(UTC)
    values = deepcopy(data.inventory_snapshot.as_dict())
    values["CAM001"].update(changes)
    if "alarm_notify" in changes:
        values["CAM001"].setdefault("STATUS", {})["globalStatus"] = changes["alarm_notify"]
    states = {
        serial: type(state)(
            serial,
            state.kind,
            values[serial],
            replace(state.capabilities, observed=state.capabilities.observed | {"alarm_notify"})
            if serial == "CAM001" and "alarm_notify" in changes
            else state.capabilities,
        )
        for serial, state in data.inventory_snapshot.devices.items()
    }
    return DeviceSnapshot(
        data.inventory_snapshot.revision + 1,
        now,
        states,
        {serial: DeviceHealth(True, True, now) for serial in states},
    )


def control_data(hass, result):
    _, api, data, cam, *_ = entities(hass)
    initial = snapshot(
        data, switches={1: 0}, alarm_light_luminance=10, numeric=10, selection=0, alarm_notify=0
    )
    now = datetime.now(UTC)
    devices = dict(initial.devices)
    devices["PLUG"] = PlugState(
        "PLUG",
        "plug",
        {
            "name": "Plug",
            "device_sub_category": "plug",
            "version": "1",
            "status": 1,
            "is_on": False,
        },
    )
    devices["BULB"] = LightState(
        "BULB",
        "light",
        {
            "name": "Bulb",
            "device_sub_category": "bulb",
            "version": "1",
            "status": 1,
            "is_on": False,
            "productId": "synthetic",
            "featureItems": [{"itemKey": "light_switch"}],
        },
    )
    data.inventory_snapshot = DeviceSnapshot(
        1, now, devices, {serial: DeviceHealth(True, True, now) for serial in devices}
    )
    data.inventory_data = data.inventory_snapshot.as_dict()
    data.data = {"CAM001": deepcopy(data.inventory_data["CAM001"])}
    command = MagicMock(return_value=result)
    return api, data, cam, command


def controls(hass, kind="switch", result=True):
    api, data, cam, command = control_data(hass, result)
    if kind == "switch":
        entity = switch.EzvizSwitch(
            data,
            "CAM001",
            switch.EzvizSwitchEntityDescription(
                key="test", value_fn=lambda d: d["switches"][1], method=command
            ),
        )
        write = entity.async_turn_on

        def read():
            return entity.is_on

        target, old = True, False
        matching = snapshot(data, switches={1: 1})
    elif kind == "light":
        entity = light.EzvizLight(
            data,
            "CAM001",
            light.EzvizLightEntityDescription(
                key="test",
                is_on_value=lambda d: d["switches"][1],
                brightness_value=lambda d: d["alarm_light_luminance"],
                power_on=command,
                power_off=command,
                set_brightness=command,
            ),
        )

        def write():
            return entity.async_turn_on(brightness=205)

        def read():
            return (entity.is_on, entity.brightness)

        target, old = (True, 204), (False, 26)
        matching = snapshot(data, switches={1: 1}, alarm_light_luminance=80)
    elif kind == "number":
        entity = number.EzvizNumber(
            data,
            "CAM001",
            number.EzvizNumberEntityDescription(
                key="test", get_value=lambda d: d["numeric"], set_value=command
            ),
        )

        def write():
            return entity.async_set_native_value(75)

        def read():
            return entity.native_value

        target, old = 75, 10
        matching = snapshot(data, numeric=75)
    elif kind == "select":
        entity = select.EzvizSelect(
            data,
            "CAM001",
            select.EzvizSelectEntityDescription(
                key="test",
                options=["old", "new"],
                option_range=[0, 1],
                get_current_option=lambda d: d["selection"],
                set_current_option=command,
            ),
        )

        def write():
            return entity.async_select_option("new")

        def read():
            return entity.current_option

        target, old = "new", "old"
        matching = snapshot(data, selection=1)
    elif kind == "name":
        api.update_device_name = command
        entity = text.EzvizCameraNameText(data, "CAM001")

        def write():
            return entity.async_set_value("New name")

        def read():
            return entity.native_value

        target, old = "New name", data.data["CAM001"]["name"]
        matching = snapshot(data, name="New name")
    elif kind == "motion":
        api.set_camera_defence = command
        entity = cam
        write = entity.async_enable_motion_detection

        def read():
            return entity.motion_detection_enabled

        target, old = True, False
        matching = snapshot(data, alarm_notify=1)
    else:
        api.set_switch = api.set_device_feature_by_key = command
        serial = "PLUG" if kind == "plug" else "BULB"
        entity = (
            switch.EzvizInventoryPlug(data, serial)
            if kind == "plug"
            else light.EzvizInventoryLight(data, serial)
        )
        write = entity.async_turn_on

        def read():
            return entity.is_on

        target, old = True, False
        matching = snapshot(data)
        replacement = dict(matching.devices)
        replacement[serial] = type(replacement[serial])(
            serial, replacement[serial].kind, {**replacement[serial].values, "is_on": True}
        )
        matching = replace(matching, devices=replacement)
    return entity, api, data, command, write, read, target, old, matching


KINDS = ("switch", "light", "number", "select", "name", "motion", "plug", "bulb")


@pytest.mark.parametrize("kind", KINDS)
async def test_display_follows_only_stale_matching_and_external_observations(hass, kind):
    entity, api, data, command, write, read, target, old, matching = controls(hass, kind)
    original = data.inventory_snapshot
    stale = snapshot(data)
    api.poll_devices = MagicMock(side_effect=[stale, stale, matching, stale])
    try:
        assert read() == old
        await write()
        assert command.called and read() == old
        assert data.inventory_snapshot is original
        assert data.data["CAM001"] == original.as_dict()["CAM001"]
        api.poll_devices.assert_not_called()
        for _ in range(2):
            await data.async_refresh()
            assert read() == old
        data.merge_mqtt_update("CAM001", {"ext": {"image": "https://images.example.invalid/new"}})
        assert read() == old
        await data.async_refresh()
        assert read() == target
        await data.async_refresh()
        assert read() == old
        assert entity.available
    finally:
        await data.async_close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", [False, OSError("synthetic")])
async def test_rejected_or_failed_write_does_not_schedule_refresh(hass, kind, failure):
    _, _, data, command, write, read, _, old, _ = controls(hass, kind)
    if isinstance(failure, Exception):
        command.side_effect = failure
    else:
        command.return_value = failure
    try:
        with pytest.raises((HomeAssistantError, OSError)):
            await write()
        assert read() == old
        assert data.settings._refresh_cancel is None
    finally:
        await data.async_close()


async def test_shared_delayed_refresh_coalesces_commands(hass, monkeypatch):
    entity, api, data, _, write, _, _, _, matching = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.02)
    api.poll_devices = MagicMock(return_value=matching)
    try:
        await write()
        await entity.async_turn_off()
        await write()
        api.poll_devices.assert_not_called()
        async with asyncio.timeout(2):
            while not api.poll_devices.called or data.settings._refresh_task:
                await asyncio.sleep(0.005)
        api.poll_devices.assert_called_once()
    finally:
        await data.async_close()


@pytest.mark.parametrize("failure", ["offline", "poll", "auth", "missing"])
async def test_write_does_not_mask_device_or_poll_unavailability(hass, failure):
    entity, api, data, _, write, _, _, _, matching = controls(hass)
    try:
        await write()
        if failure in ("poll", "auth"):
            error = (
                OSError("synthetic") if failure == "poll" else ConfigEntryAuthFailed("synthetic")
            )
            api.poll_devices = MagicMock(side_effect=error)
        elif failure == "offline":
            api.poll_devices = MagicMock(return_value=snapshot(data, switches={1: 1}, status=0))
        else:
            api.poll_devices = MagicMock(return_value=replace(matching, devices={}, health={}))
        await data.async_refresh()
        assert not entity.available
    finally:
        await data.async_close()


async def test_unload_cancels_refresh_and_rejects_new_writes(hass):
    _, api, data, _, write, read, _, old, _ = controls(hass)
    api.poll_devices = MagicMock()
    await write()
    assert data.settings._refresh_cancel
    await data.async_close()
    assert read() == old
    assert data.settings._refresh_cancel is None
    api.poll_devices.assert_not_called()
    with pytest.raises(HomeAssistantError):
        await write()


@pytest.mark.parametrize("kind", ["number", "select"])
async def test_sdk_writer_receives_detached_camera_data(hass, kind):
    _, _, data, command, write, _, _, _, _ = controls(hass, kind)
    before = deepcopy(data.data)

    def mutate(*args):
        args[-1]["numeric"] = 999
        args[-1]["switches"][1] = 1

    command.side_effect = mutate  # A void-returning SDK setter is accepted.
    try:
        await write()
        assert data.data == before
    finally:
        await data.async_close()


async def test_cancelled_service_still_commits_accepted_write_and_orders_next(hass):
    entity, _, data, command, write, read, _, _, _ = controls(hass)
    entered, release = Event(), Event()

    def blocked(*_args):
        entered.set()
        assert release.wait(5)
        return True

    command.side_effect = blocked
    first = asyncio.create_task(write())
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(entity.async_turn_off())
        await asyncio.sleep(0)
        command.assert_called_once()
        release.set()
        await second
        assert command.call_args_list[0].args[-1] == 1
        assert command.call_args_list[1].args[-1] == 0
        assert read() is False
    finally:
        release.set()
        if second:
            await second
        await data.async_close()


async def test_slow_compound_write_restarts_delay_after_latest_ack(hass, monkeypatch):
    _, _, data, command, write, _, _, _, _ = controls(hass, "light")
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.03)
    entered, release = Event(), Event()

    def slow_power(*args):
        if len(args) == 2:
            entered.set()
            assert release.wait(5)
        return True

    command.side_effect = slow_power
    refresh = AsyncMock()
    monkeypatch.setattr(data, "async_request_refresh", refresh)
    task = asyncio.create_task(write())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.05)  # First timer is waiting behind the power write.
        refresh.assert_not_awaited()
        release.set()
        await task
        refresh.assert_not_awaited()  # It must not poll immediately after ACK.
        async with asyncio.timeout(2):
            while not refresh.await_count:
                await asyncio.sleep(0.005)
        refresh.assert_awaited_once()
    finally:
        release.set()
        await task
        await data.async_close()


async def test_unload_during_delayed_refresh_waits_for_poll_worker(hass, monkeypatch):
    _, api, data, _, write, _, _, _, matching = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.01)
    entered, release = Event(), Event()

    def poll(_options):
        entered.set()
        assert release.wait(5)
        return matching

    api.poll_devices = MagicMock(side_effect=poll)
    close = None
    try:
        await write()
        assert await asyncio.to_thread(entered.wait, 2)
        close = asyncio.create_task(data.async_close())
        await asyncio.sleep(0)
        assert not close.done()
        api.close_session.assert_not_called()
        release.set()
        await close
        api.close_session.assert_called_once()
        assert data.settings._refresh_cancel is None
    finally:
        release.set()
        if close:
            await close
        await data.async_close()


async def test_auth_failed_refresh_has_no_retry_loop(hass, monkeypatch):
    _, api, data, _, write, _, _, _, _ = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.01)
    api.poll_devices = MagicMock(side_effect=ConfigEntryAuthFailed("synthetic"))
    try:
        await write()
        async with asyncio.timeout(2):
            while data.last_update_success or data.settings._refresh_task:
                await asyncio.sleep(0.005)
        assert data.settings._refresh_cancel is None
        api.poll_devices.assert_called_once()
    finally:
        await data.async_close()


async def test_different_controls_share_one_poll_but_accounts_are_independent(hass, monkeypatch):
    _, api, data, _, write, _, _, _, _ = controls(hass)
    _, other_api, other_account, _, other_write, _, _, _, _ = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.02)
    extra = number.EzvizNumber(
        data, "CAM001",
        number.EzvizNumberEntityDescription(
            key="test", get_value=lambda d: d["numeric"], set_value=lambda *_: True
        ),
    )
    api.poll_devices = MagicMock(return_value=snapshot(data, switches={1: 1}, numeric=75))
    other_api.poll_devices = MagicMock(return_value=snapshot(other_account))
    try:
        await write()
        await extra.async_set_native_value(75)
        await other_write()
        assert extra.native_value == 10
        async with asyncio.timeout(2):
            while not api.poll_devices.called or not other_api.poll_devices.called:
                await asyncio.sleep(0.005)
            while data.settings._refresh_task or other_account.settings._refresh_task:
                await asyncio.sleep(0.005)
        api.poll_devices.assert_called_once()
        other_api.poll_devices.assert_called_once()
        assert extra.native_value == 75
    finally:
        await data.async_close()
        await other_account.async_close()


async def test_unrenderable_property_cannot_fail_inventory_poll(hass):
    _, api, data, _, write, _, _, _, matching = controls(hass, "number")
    values = dict(matching.devices["CAM001"].values)
    values.pop("numeric")
    states = {**matching.devices, "CAM001": CameraState("CAM001", "camera", values)}
    api.poll_devices = MagicMock(return_value=replace(matching, devices=states))
    try:
        await write()
        await data.async_refresh()
        assert data.last_update_success
    finally:
        await data.async_close()


async def test_write_during_inflight_refresh_gets_a_followup(hass, monkeypatch):
    _, _, data, _, write, _, _, _, _ = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(data, "async_request_refresh", refresh)
    try:
        await write()
        await asyncio.wait_for(entered.wait(), 2)
        await write()
        await asyncio.sleep(0.03)
        assert calls == 1
        release.set()
        async with asyncio.timeout(2):
            while calls < 2 or data.settings._refresh_task:
                await asyncio.sleep(0.005)
        assert calls == 2
    finally:
        release.set()
        await data.async_close()


async def test_stale_refresh_does_not_start_retries(hass, monkeypatch):
    _, api, data, _, write, read, _, old, _ = controls(hass)
    monkeypatch.setattr(setting_writes, "REFRESH_DELAY", 0.01)
    api.poll_devices = MagicMock(return_value=snapshot(data))
    try:
        await write()
        async with asyncio.timeout(2):
            while not api.poll_devices.called or data.settings._refresh_task:
                await asyncio.sleep(0.005)
        assert read() == old
        assert data.settings._refresh_cancel is None
        assert not data.settings._refresh_due
        api.poll_devices.assert_called_once()
    finally:
        await data.async_close()
