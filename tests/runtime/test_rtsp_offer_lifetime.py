"""A retained SDK row is not authority to consume an old local enable offer."""

import asyncio
from copy import copy
from dataclasses import replace
from threading import Event
from unittest.mock import MagicMock

from pyezvizapi import DevicePollOptions, EzvizClient
from pyezvizapi.devices.snapshot import DeviceHealth
from pyezvizapi.exceptions import PyEzvizError
import pytest

from custom_components.ezviz_cloud import options_flow
from homeassistant.data_entry_flow import FlowResultType

from . import test_rtsp_setup as setup
from .test_local_rtsp import state

sdk = setup.sdk
probe = setup.probe
flow_setup = setup.flow_setup


async def remove_via_sdk(account, api):
    """Use the real SDK's historical-row retention, not dictionary deletion."""
    client = EzvizClient()
    try:
        client._device_snapshot = account.inventory_snapshot
        client.get_device_records = MagicMock(return_value={})
        snapshot = client.poll_devices(DevicePollOptions(include_camera_alarms=False))
    finally:
        client.close_session()
    assert snapshot.health["CAM001"].present is False
    assert snapshot.health["CAM001"].error_type == "MissingFromInventory"
    api.poll_devices = MagicMock(return_value=snapshot)
    await account.async_refresh()
    assert "CAM001" in account.data and account.last_update_success
    assert account.inventory_snapshot.health["CAM001"].present is False


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("invalid", ["removed", "runtime"])
@pytest.mark.parametrize("enable", [False, True])
async def test_offer_rejects_removed_camera_or_replaced_owner(hass, sdk, probe, flow_setup, queued, invalid, enable):
    client, _ = sdk
    entry, api, account, _ = flow_setup
    original_options = entry.options
    entered, release = Event(), Event()
    blocker = task = None
    def block(*_args):
        entered.set()
        assert release.wait(5)
    try:
        result = await setup.submit(hass, entry, ephemeral_test_rtsp=True)
        client.services_switch_state.side_effect = [state(0), state(1)]
        if queued and enable:
            blocker = asyncio.create_task(account.local_device("CAM001").async_execute(block, options={**setup.CAMERA, setup.AUTH: "verification_code"}))
            assert await asyncio.to_thread(entered.wait, 2)
            task = asyncio.create_task(hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": enable}))
            await asyncio.sleep(0)
        if invalid == "removed":
            await remove_via_sdk(account, api)
        else:
            entry.runtime_data = copy(entry.runtime_data)
        if task:
            release.set()
            await blocker
            result = await task
        else:
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": enable})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == ("no_cameras" if invalid == "removed" else "integration_unavailable")
        client.services_switch_set.assert_not_called()
        probe.assert_not_called()
        assert entry.options == original_options
    finally:
        release.set()
        if blocker:
            await blocker
        if task:
            await task
        await account.async_close()


@pytest.mark.parametrize("phase", ["write", "probe"])
async def test_removal_before_probe_or_commit_aborts_without_option_save(hass, sdk, probe, flow_setup, monkeypatch, phase):
    client, _ = sdk
    entry, api, account, _ = flow_setup
    before = entry.options
    try:
        result = await setup.submit(hass, entry, ephemeral_test_rtsp=True)
        if phase == "write":
            device = account.local_device("CAM001")
            original = device.async_execute
            async def after_write(*args, **kwargs):
                value = await original(*args, **kwargs)
                await remove_via_sdk(account, api)
                return value
            monkeypatch.setattr(device, "async_execute", after_write)
        else:
            original = options_flow.EzvizOptionsFlowHandler._probe_rtsp
            async def after_probe(handler, resolved):
                await original(handler, resolved)
                await remove_via_sdk(account, api)
            monkeypatch.setattr(options_flow.EzvizOptionsFlowHandler, "_probe_rtsp", after_probe)
        client.services_switch_state.side_effect = [state(0), state(1)]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_cameras"
        assert entry.options == before
        client.services_switch_set.assert_called_once()
        assert probe.call_count == int(phase == "probe")
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure", ["stale", "cloud", "unknown"])
async def test_stale_health_or_cloud_failure_does_not_invalidate_independent_lan(hass, sdk, probe, flow_setup, failure):
    client, _ = sdk
    entry, api, account, _ = flow_setup
    try:
        result = await setup.submit(hass, entry, ephemeral_test_rtsp=True)
        if failure == "stale":
            previous = account.inventory_snapshot.health["CAM001"]
            account.inventory_snapshot = replace(account.inventory_snapshot, health={"CAM001": DeviceHealth(True, False, previous.last_success, "MalformedDevice")})
        elif failure == "unknown":
            account.inventory_snapshot = None
        else:
            api.poll_devices = MagicMock(side_effect=PyEzvizError("synthetic cloud outage"))
            await account.async_refresh()
            assert not account.last_update_success
        client.services_switch_state.side_effect = [state(0), state(1)]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        client.services_switch_set.assert_called_once()
        probe.return_value.main.assert_called_once()
    finally:
        await account.async_close()
