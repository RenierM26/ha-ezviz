"""Cloud propagation delay with real coordinator polling and pending settings."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi import DeviceSnapshot
from pyezvizapi.devices.snapshot import CameraState, DeviceHealth, LightState, PlugState
import pytest

from custom_components.ezviz_cloud import confirmation, light, number, select, switch, text
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
async def test_stale_stale_matching_then_external_change(hass, kind):
    entity, api, data, command, write, read, target, old, matching = controls(hass, kind)
    original = data.inventory_snapshot
    stale = snapshot(data)
    api.poll_devices = MagicMock(side_effect=[stale, stale, matching, stale])
    try:
        assert read() == old
        await write()
        assert command.called and read() == target
        assert data.inventory_snapshot is original
        assert data.data["CAM001"] == original.as_dict()["CAM001"]
        assert data.settings.pending
        api.poll_devices.assert_not_called()  # No immediate stale poll/cooldown.
        for _ in range(2):
            await data.async_refresh()
            assert read() == target and data.settings.pending
        data.merge_mqtt_update("CAM001", {"ext": {"image": "https://images.example.invalid/new"}})
        assert read() == target and data.settings.pending
        await data.async_refresh()
        assert read() == target and not data.settings.pending
        await data.async_refresh()
        assert read() == old  # No permanent optimism after confirmation.
        assert entity.available
    finally:
        await data.async_close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", [False, OSError("synthetic")])
async def test_rejected_or_failed_write_never_creates_pending(hass, kind, failure):
    _, _, data, command, write, read, _, old, _ = controls(hass, kind)
    if isinstance(failure, Exception):
        command.side_effect = failure
    else:
        command.return_value = failure
    try:
        with pytest.raises((HomeAssistantError, OSError)):
            await write()
        assert read() == old and not data.settings.pending
        assert data.settings._confirm_cancel is None
    finally:
        await data.async_close()


async def test_opposite_commands_fence_precommand_poll_and_supersede_pending(hass):
    entity, api, data, _, write, read, _, _, matching = controls(hass)
    entered, release = Event(), Event()

    def poll(_options):
        entered.set()
        assert release.wait(5)
        return matching

    api.poll_devices = MagicMock(side_effect=poll)
    poll_task = asyncio.create_task(data.async_refresh())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await write()
        assert read() is True
        release.set()
        await poll_task
        assert read() is True and data.settings.pending  # Old poll cannot confirm.
        await entity.async_turn_off()
        latest = data.settings.generation
        assert read() is False
        api.poll_devices.side_effect = None
        api.poll_devices.return_value = matching
        await data.async_refresh()
        assert read() is False and data.settings.pending
        assert all(item.generation == latest for item in data.settings.pending.values())
        api.poll_devices.return_value = snapshot(data, switches={1: 0})
        await data.async_refresh()
        assert read() is False and not data.settings.pending
    finally:
        release.set()
        await poll_task
        await data.async_close()


async def test_shared_delayed_refresh_coalesces_commands(hass, monkeypatch):
    entity, api, data, _, write, _, _, _, matching = controls(hass)
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.02)
    api.poll_devices = MagicMock(return_value=matching)
    try:
        await write()
        await entity.async_turn_off()
        await write()
        api.poll_devices.assert_not_called()
        async with asyncio.timeout(2):
            while data.settings.pending:
                await asyncio.sleep(0.005)
        api.poll_devices.assert_called_once()
    finally:
        await data.async_close()


async def test_pending_expiry_publishes_observed_value_without_poll(hass, monkeypatch):
    entity, _api, data, _, write, read, _, old, _ = controls(hass)
    monkeypatch.setattr(confirmation, "PENDING_TIMEOUT", 0.02)
    notify = MagicMock()
    remove = data.async_add_listener(notify)
    try:
        await write()
        assert read() is True
        async with asyncio.timeout(2):
            while data.settings.pending:
                await asyncio.sleep(0.005)
        assert read() == old and entity.available
        assert notify.call_count == 2  # Acceptance and expiry, no fake snapshot.
        assert data.settings._confirm_cancel is None
    finally:
        remove()
        await data.async_close()


@pytest.mark.parametrize("failure", ["offline", "poll", "auth", "missing"])
async def test_pending_does_not_mask_availability_or_confirm_failed_device(hass, failure):
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
        assert not entity.available and data.settings.pending
    finally:
        await data.async_close()


async def test_unload_cancels_both_callbacks_and_rejects_new_writes(hass):
    _, api, data, _, write, read, _, old, _ = controls(hass)
    api.poll_devices = MagicMock()
    await write()
    assert data.settings._confirm_cancel and data.settings._expiry_cancel
    await data.async_close()
    assert not data.settings.pending and read() == old
    assert data.settings._confirm_cancel is data.settings._expiry_cancel is None
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
        assert data.settings.pending and data.data == before
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
        assert data.settings.generation == 2 and read() is False
    finally:
        release.set()
        if second:
            await second
        await data.async_close()


async def test_slow_compound_write_restarts_delay_after_latest_ack(hass, monkeypatch):
    _, _, data, command, write, _, _, _, _ = controls(hass, "light")
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.03)
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


async def test_unload_during_confirmation_waits_for_poll_worker(hass, monkeypatch):
    _, api, data, _, write, _, _, _, matching = controls(hass)
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.01)
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
        assert not data.settings.pending
        assert data.settings._confirm_cancel is data.settings._expiry_cancel is None
    finally:
        release.set()
        if close:
            await close
        await data.async_close()


async def test_auth_failed_confirmation_has_no_retry_loop(hass, monkeypatch):
    _, api, data, _, write, _, _, _, _ = controls(hass)
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.01)
    api.poll_devices = MagicMock(side_effect=ConfigEntryAuthFailed("synthetic"))
    try:
        await write()
        async with asyncio.timeout(2):
            while data.last_update_success or data.settings._refresh_task:
                await asyncio.sleep(0.005)
        assert data.settings.pending
        assert data.settings._confirm_cancel is None
        api.poll_devices.assert_called_once()
    finally:
        await data.async_close()


async def test_timed_out_precommand_worker_retains_original_confirmation_fence(hass):
    _, api, data, _, write, _, _, _, matching = controls(hass)
    entered, release = Event(), Event()
    data._api_timeout = 0.01

    def poll(_options):
        entered.set()
        assert release.wait(5)
        return matching

    api.poll_devices = MagicMock(side_effect=poll)
    second = None
    try:
        await data.async_refresh()
        assert entered.is_set() and not data.last_update_success
        await write()
        data._api_timeout = 5
        second = asyncio.create_task(data.async_refresh())
        await asyncio.sleep(0)
        release.set()
        await second
        api.poll_devices.assert_called_once()
        assert data.settings.pending  # Reused old worker cannot confirm new write.
        api.poll_devices.side_effect = None
        api.poll_devices.return_value = matching
        await data.async_refresh()
        assert not data.settings.pending
    finally:
        release.set()
        if second:
            await second
        await data.async_close()


async def test_scheduled_retries_keep_pending_until_network_observation_matches(hass, monkeypatch):
    _, api, data, _, write, read, _, _, matching = controls(hass)
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.01)
    monkeypatch.setattr(confirmation, "CONFIRMATION_RETRY", 0.02)
    data._debounced_refresh.cooldown = 0.01  # Real Core debouncer, accelerated time.
    stale = snapshot(data)
    api.poll_devices = MagicMock(side_effect=[stale, stale, matching])
    try:
        await write()
        async with asyncio.timeout(2):
            while data.settings.pending:
                assert read() is True
                await asyncio.sleep(0.005)
        assert read() is True
        assert api.poll_devices.call_count == 3
        assert data.settings._confirm_cancel is None
    finally:
        await data.async_close()


async def test_different_controls_share_one_poll_but_not_another_accounts_pending(
    hass, monkeypatch
):
    _, api, data, _, write, _, _, _, _ = controls(hass)
    _, _, other_account, _, other_write, _, _, _, _ = controls(hass)
    monkeypatch.setattr(confirmation, "CONFIRMATION_DELAY", 0.02)
    extra = number.EzvizNumber(
        data,
        "CAM001",
        number.EzvizNumberEntityDescription(
            key="test",
            get_value=lambda d: d["numeric"],
            set_value=lambda *_: True,
        ),
    )
    api.poll_devices = MagicMock(return_value=snapshot(data, switches={1: 1}, numeric=75))
    try:
        await write()
        await extra.async_set_native_value(75)
        await other_write()
        # Keep the other account's poll outside this test; its state is independent.
        other_account.settings._confirm_cancel()
        other_account.settings._confirm_cancel = None
        assert len(data.settings.pending) == 2
        async with asyncio.timeout(2):
            while data.settings.pending:
                await asyncio.sleep(0.005)
        api.poll_devices.assert_called_once()
        assert other_account.settings.pending
        assert extra.native_value == 75
    finally:
        await data.async_close()
        await other_account.async_close()


async def test_unrenderable_confirmation_property_cannot_fail_inventory_poll(hass):
    _, api, data, _, write, _, _, _, matching = controls(hass, "number")
    values = dict(matching.devices["CAM001"].values)
    values.pop("numeric")
    states = {**matching.devices, "CAM001": CameraState("CAM001", "camera", values)}
    api.poll_devices = MagicMock(return_value=replace(matching, devices=states))
    try:
        await write()
        await data.async_refresh()
        assert data.last_update_success and data.settings.pending
    finally:
        await data.async_close()
