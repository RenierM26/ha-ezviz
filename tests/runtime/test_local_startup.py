"""Optional LAN startup is bounded, background and independent of cloud controls."""

import asyncio
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ezviz_cloud import local, switch

from .core_policy.test_local_rtsp import CONFIG
from .test_hardening import entities
from .test_local_rtsp import state


@pytest.mark.parametrize("reachable", [False, True])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cloud_publication_does_not_wait_for_blocked_lan(hass, monkeypatch, reachable, shutdown):  # noqa: PLR0915 - Complete setup/worker/unload lifecycle.
    entry, api, account, *_ = entities(hass)
    account.data = {f"CAM00{n}": {**account.data["CAM001"], "switches": {1: 0}} for n in [1, 2, 3]}
    entry.runtime_data = SimpleNamespace(coordinator=account)
    hass.config_entries.async_update_entry(entry, options={"cameras": {serial: CONFIG for serial in account.data}})
    entered, release, lock = Event(), Event(), Lock()
    active = peak = started = 0
    client = MagicMock()
    def read():
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        try:
            assert release.wait(5)
            if not reachable:
                raise OSError("synthetic LAN failure")
            return state(0)
        finally:
            with lock:
                active -= 1
    client.services_switch_state.side_effect = read
    monkeypatch.setattr(local, "HcNetSdkPurePythonClient", lambda *_a, **_k: client)
    monkeypatch.setattr(switch, "migrate_unique_ids_with_coordinator", AsyncMock())
    added = []
    closing = None
    try:
        async with asyncio.timeout(0.5):
            await switch.async_setup_entry(hass, entry, added.extend)
        cloud = [entity for entity in added if isinstance(entity, switch.EzvizSwitch)]
        local_entities = [entity for entity in added if isinstance(entity, switch.EzvizRtspSwitch)]
        assert cloud and len(local_entities) == 3
        assert all(not entity.available and entity.is_on is None for entity in local_entities)
        client.services_switch_state.assert_not_called()
        assert await asyncio.to_thread(entered.wait, 2)
        assert peak == started == 2
        if shutdown:
            await entry._async_process_on_unload(hass)
            assert not entry._background_tasks
            closing = asyncio.create_task(account.async_close())
            await asyncio.sleep(0)
            assert not closing.done()
            api.close_session.assert_not_called()
        release.set()
        if closing:
            await closing
            assert started == 2  # A canceled queued initializer never dispatches.
            api.close_session.assert_called_once()
        else:
            await asyncio.gather(*tuple(entry._background_tasks))
            assert started == 3 and peak == 2
            assert all(entity.available is reachable for entity in local_entities)
            if reachable:
                assert all(entity.is_on is False for entity in local_entities)
        client.services_switch_set.assert_not_called()
    finally:
        release.set()
        await entry._async_process_on_unload(hass)
        if closing:
            await closing
        await account.async_close()
