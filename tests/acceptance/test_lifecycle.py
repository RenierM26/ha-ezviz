"""Real HA storage/lifecycle controls, including failed fsync and late workers."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import atomicwrites
from pyezvizapi.exceptions import EzvizAuthTokenExpired, PyEzvizError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import diagnostics, mqtt, runtime, tokens
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.tokens import TokenPersistence
from homeassistant.core import CoreState


def entry_for(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"session_id": "synthetic", "user_id": "user", "url": "api.invalid"}
    )
    entry.add_to_hass(hass)
    return entry


async def test_real_atomic_store_waits_for_fsync_private_mode_and_reloads(hass, monkeypatch):
    entry = entry_for(hass)
    persistence = TokenPersistence(hass, entry)
    entered, release = Event(), Event()
    original = atomicwrites._proper_fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", fsync)
    token = {"session_id": "new-synthetic", "push_state": {"counter": 2}}
    expected = deepcopy(token)
    task = asyncio.ensure_future(hass.async_add_executor_job(persistence.save_from_worker, token))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        token["push_state"]["counter"] = 3
        assert not task.done()
    finally:
        release.set()
    await task
    assert len(calls) >= 2  # file + directory sync
    path = Path(persistence._store.path)
    assert path.stat().st_mode & 0o077 == 0
    assert json.loads(path.read_text())["data"]["token"] == expected
    assert await TokenPersistence(hass, entry).async_load() == expected
    await persistence.async_remove()
    assert not path.exists()


async def test_real_store_disk_failure_is_not_acknowledged(hass, monkeypatch):
    persistence = TokenPersistence(hass, entry_for(hass))

    def fail(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(atomicwrites, "_proper_fsync", fail)
    with pytest.raises(OSError, match="durable token write failed"):
        await hass.async_add_executor_job(persistence.save_from_worker, {"session_id": "synthetic"})
    assert not Path(persistence._store.path).exists()


async def test_shutdown_deferred_store_write_is_not_acknowledged(hass):
    persistence = TokenPersistence(hass, entry_for(hass))
    prior = hass.state
    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(OSError, match="did not complete"):
            await hass.async_add_executor_job(
                persistence.save_from_worker, {"session_id": "synthetic"}
            )
    finally:
        hass.set_state(prior)
        await persistence.async_remove()


async def test_failed_worker_monitor_recovers_and_shutdown_owns_cleanup(hass, monkeypatch):
    entry = entry_for(hass)
    entry.runtime_data = SimpleNamespace(coordinator=MagicMock())
    sdk = MagicMock()
    sdk.raise_if_failed.side_effect = [PyEzvizError("PRIVATE_CANARY"), None]
    client = SimpleNamespace(get_mqtt_client=MagicMock(return_value=sdk))
    handler = mqtt.EzvizMqttHandler(hass, client, entry.entry_id)
    monkeypatch.setattr(mqtt, "PUSH_HEALTH_SECONDS", 0.001)
    handler.async_start()
    await handler._task
    first_health = handler._health_task
    await asyncio.wait_for(first_health, 2)
    await asyncio.wait_for(handler._task, 2)
    assert sdk.connect.call_count == 2 and sdk.stop.call_count == 1
    await handler.async_wait_stopped()
    assert sdk.stop.call_count == 2


async def test_legacy_auth_requests_one_reauth_without_disabling_polling(hass, monkeypatch):
    entry = entry_for(hass)
    data = SimpleNamespace(data={"known": {"status": 1}}, last_update_success=True)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    client = SimpleNamespace(
        get_mqtt_client=MagicMock(side_effect=EzvizAuthTokenExpired("enable_channel99"))
    )
    request = MagicMock()
    monkeypatch.setattr(type(entry), "async_start_reauth", request)
    monkeypatch.setattr(mqtt, "PUSH_RETRY_SECONDS", 0.001)
    handler = mqtt.EzvizMqttHandler(hass, client, entry.entry_id)
    handler.async_start()
    await asyncio.wait_for(handler._task, 2)
    await asyncio.sleep(0.025)  # Multiple retry intervals must not repeat fatal auth.
    assert client.get_mqtt_client.call_count == 1
    assert request.call_count == 1 and data.last_update_success
    await handler.async_wait_stopped()


async def test_runtime_waits_for_late_login_and_push_before_http_close(hass, monkeypatch):
    entered, release = Event(), Event()
    order = []

    def login():
        entered.set()
        assert release.wait(5)
        order.append("login")

    coordinator = SimpleNamespace(
        begin_shutdown=MagicMock(), async_close=AsyncMock(side_effect=lambda: order.append("http"))
    )
    push = SimpleNamespace(async_wait_stopped=AsyncMock(side_effect=lambda: order.append("push")))
    owner = runtime.RuntimeCleanup(hass, coordinator, push)
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.01)
    task = asyncio.create_task(owner.async_run(login))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.async_stop()
        assert not owner.task.done() and not order
    finally:
        release.set()
    await owner.task
    assert order == ["login", "push", "http"]
    await owner.async_stop()
    coordinator.async_close.assert_awaited_once()


async def test_diagnostics_are_cached_and_allowlisted(hass):
    entry = entry_for(hass)
    client = SimpleNamespace(
        get_device_infos=MagicMock(side_effect=AssertionError("no extra query"))
    )
    data = SimpleNamespace(
        inventory_snapshot=None,
        inventory_data={},
        last_update_success=True,
        data={
            "PRIVATE_SERIAL": {
                "status": 1,
                "name": "PRIVATE_NAME",
                "session_id": "PRIVATE_TOKEN",
                "last_alarm_pic": "PRIVATE_URL",
                "push_state": "PRIVATE_KEYS",
            }
        },
        ezviz_client=client,
    )
    sdk = SimpleNamespace(
        get_push_diagnostics=lambda: {
            "state": "online",
            "session": "PRIVATE_SESSION",
            "token": "PRIVATE_TOKEN",
        }
    )
    handler = mqtt.EzvizMqttHandler(hass, client, entry.entry_id)
    handler._mqtt = sdk
    entry.runtime_data = SimpleNamespace(coordinator=data, push=handler)
    value = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert "PRIVATE_" not in json.dumps(value)
    assert value["push"] == {"state": "online"}
    client.get_device_infos.assert_not_called()


async def test_reauth_with_same_session_replaces_old_push_state(hass):
    entry = entry_for(hass)
    first = TokenPersistence(hass, entry)
    await hass.async_add_executor_job(
        first.save_from_worker, {"session_id": "synthetic", "push_state": {"counter": 9}}
    )
    fresh = {"session_id": "synthetic", "push_state": {"counter": 0, "new_secret": "synthetic"}}
    await TokenPersistence(hass, entry).async_replace(fresh)
    assert await TokenPersistence(hass, entry).async_load() == fresh
    await first.async_remove()


async def test_timed_out_disk_write_cannot_recreate_removed_credentials(hass, monkeypatch):
    entry = entry_for(hass)
    persistence = TokenPersistence(hass, entry)
    entered, release = Event(), Event()
    original = atomicwrites._proper_fsync

    def slow(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", slow)
    monkeypatch.setattr(tokens, "PERSIST_TIMEOUT", 0.01)
    owner = runtime.RuntimeCleanup(
        hass,
        SimpleNamespace(begin_shutdown=MagicMock(), async_close=AsyncMock()),
        persistence=persistence,
    )
    hass.data[DOMAIN] = {"_runtimes": {entry.entry_id: owner}}
    try:
        with pytest.raises(TimeoutError):
            await hass.async_add_executor_job(
                persistence.save_from_worker, {"session_id": "synthetic"}
            )
        assert await asyncio.to_thread(entered.wait, 2)
        assert persistence._pending
        await ezviz_cloud.async_remove_entry(hass, entry)
        await asyncio.sleep(0.02)
        assert owner.task is not None and not owner.task.done()
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not Path(persistence._store.path).exists()
    assert not persistence._pending
    with pytest.raises(OSError, match="removed"):
        await hass.async_add_executor_job(persistence.save_from_worker, {"session_id": "late"})
