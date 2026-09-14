"""Integration boundaries exercised against an installed Home Assistant runtime."""

import asyncio
import json
from threading import Event
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from pyezvizapi.exceptions import EzvizAuthVerificationCode

import custom_components.ezviz_cloud as integration
from custom_components.ezviz_cloud import config_flow
from custom_components.ezviz_cloud.const import CONF_TOKEN
from custom_components.ezviz_cloud.runtime import EzvizRuntimeData
from custom_components.ezviz_cloud.token_store import EzvizTokenStore
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util.file import WriteError


def credentials():
    """Synthetic full token; never use live secrets in these tests."""
    return {"session_id": "initial", "rf_session_id": "refresh", "username": "internal",
            "api_url": "api.example.invalid", "user_id": "user", "feature_code": "host",
            "push_profile": "android-channel99", "push_state": {"device_id": "synthetic"}}


@pytest.mark.asyncio
async def test_real_storage_commits_private_complete_snapshot(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="synthetic-entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    snapshot = {**credentials(), "session_id": "rotated"}
    await asyncio.to_thread(persistence.save, snapshot)
    snapshot["push_state"]["device_id"] = "changed-after-save"
    loaded = await EzvizTokenStore(hass, entry).async_load()
    assert loaded["session_id"] == "rotated"
    assert loaded["push_state"]["device_id"] == "synthetic"
    path = tmp_path / ".storage/ezviz_cloud.synthetic-entry.token"
    assert json.loads(path.read_text())["data"]["token"] == loaded
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(RuntimeError, match="executor"):
        persistence.save(loaded)


@pytest.mark.asyncio
async def test_real_storage_write_failure_is_not_acknowledged(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    monkeypatch.setattr(persistence.store, "_write_prepared_data", Mock(side_effect=WriteError()))
    with pytest.raises(OSError, match="did not complete"):
        await asyncio.to_thread(persistence.save, credentials())


@pytest.mark.asyncio
async def test_reauth_rejects_old_saves_and_ignores_old_login_cache(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    old = EzvizTokenStore(hass, entry)
    await old.async_save(credentials())
    entry.data = {CONF_TOKEN: {**credentials(), "session_id": "reauthenticated"}}
    new = EzvizTokenStore(hass, entry)
    assert old.lock is new.lock
    with pytest.raises(RuntimeError, match="login changed"):
        await old.async_save(credentials())
    assert (await new.async_load())["session_id"] == "reauthenticated"
    path = tmp_path / ".storage/ezviz_cloud.entry.token"
    stored = json.loads(path.read_text())["data"]
    assert stored["seed"] == "reauthenticated"
    assert stored["token"] == entry.data[CONF_TOKEN]


@pytest.mark.asyncio
async def test_shutdown_does_not_acknowledge_a_deferred_save(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    hass.set_state(CoreState.stopping)
    with pytest.raises(OSError, match="stopping"):
        await persistence.async_save(credentials())


@pytest.mark.asyncio
@pytest.mark.parametrize("mfa", [False, True])
async def test_actual_ha_flow_keeps_whole_android_token_without_password(tmp_path, monkeypatch, mfa):
    hass = HomeAssistant(str(tmp_path))
    flow = config_flow.EzvizConfigFlow()
    flow.hass = hass
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", Mock())
    client = Mock()
    client.enable_channel99.side_effect = [EzvizAuthVerificationCode(), credentials()] if mfa else [credentials()]
    monkeypatch.setattr(config_flow, "EzvizClient", Mock(return_value=client))
    result = await flow.async_step_user({"username": "test@example.invalid", "password": "private",
                                         "region": config_flow.REGION_EU, "timeout": 20})
    if mfa:
        assert result["step_id"] == "user_mfa_confirm"
        result = await flow.async_step_user_mfa_confirm({"sms_code": "123456"})
    assert result["data"][CONF_TOKEN] == credentials()
    assert "password" not in result["data"]
    client.login.assert_not_called()


@pytest.mark.asyncio
async def test_new_login_save_wins_over_an_inflight_old_save(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    old = EzvizTokenStore(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    write = old.store._async_write_data

    async def blocked_write(data):
        entered.set()
        await release.wait()
        await write(data)

    monkeypatch.setattr(old.store, "_async_write_data", blocked_write)
    task = asyncio.create_task(old.async_save(credentials()))
    await entered.wait()
    entry.data = {CONF_TOKEN: {**credentials(), "session_id": "new-login"}}
    new = EzvizTokenStore(hass, entry)
    replacement = asyncio.create_task(new.async_save(entry.data[CONF_TOKEN]))
    release.set()
    with pytest.raises(RuntimeError, match="login changed"):
        await task
    await replacement
    assert (await new.async_load())["session_id"] == "new-login"


@pytest.mark.asyncio
@pytest.mark.parametrize("mfa", [False, True])
async def test_reauth_replaces_complete_token_for_the_target_entry(tmp_path, monkeypatch, mfa):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(unique_id="target@example.invalid", data={"url": "api.example.invalid"}, options={})
    flow = config_flow.EzvizConfigFlow()
    flow.hass = hass
    monkeypatch.setattr(flow, "_get_reauth_entry", Mock(return_value=entry))
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    finish = Mock(return_value={"type": "abort", "reason": "reauth_successful"})
    monkeypatch.setattr(flow, "async_update_reload_and_abort", finish)
    client = Mock()
    client.enable_channel99.side_effect = [EzvizAuthVerificationCode(), credentials()] if mfa else [credentials()]
    monkeypatch.setattr(config_flow, "EzvizClient", Mock(return_value=client))
    await flow.async_step_reauth(entry.data)
    result = await flow.async_step_reauth_confirm({"username": entry.unique_id, "password": "private"})
    if mfa:
        assert result["step_id"] == "reauth_mfa"
        result = await flow.async_step_reauth_mfa({"sms_code": "123456"})
    assert result["reason"] == "reauth_successful"
    assert finish.call_args.args == (entry,)
    assert finish.call_args.kwargs["data"][CONF_TOKEN] == credentials()
    assert "password" not in finish.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_malformed_saved_token_cannot_fall_back_to_new_registration(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    await persistence.store.async_save({"unexpected": "data"})
    with pytest.raises(OSError, match="malformed"):
        await persistence.async_load()


@pytest.mark.asyncio
async def test_corrupt_json_stays_fail_closed_across_repeated_setup(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    await persistence.async_save(credentials())
    path = tmp_path / ".storage/ezviz_cloud.entry.token"
    path.write_text("invalid json")
    for _ in range(2):
        with pytest.raises(OSError, match="decoded"):
            await EzvizTokenStore(hass, entry).async_load()
        assert path.exists()


@pytest.mark.asyncio
async def test_removal_waits_for_write_and_late_callback_cannot_recreate_file(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    entry: Any = SimpleNamespace(entry_id="entry", data={CONF_TOKEN: credentials()})
    persistence = EzvizTokenStore(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    write = persistence.store._async_write_data

    async def blocked_write(data):
        entered.set()
        await release.wait()
        await write(data)

    monkeypatch.setattr(persistence.store, "_async_write_data", blocked_write)
    saving = asyncio.create_task(persistence.async_save(credentials()))
    await entered.wait()
    removing = asyncio.create_task(EzvizTokenStore.async_remove(hass, entry.entry_id))
    await asyncio.sleep(0)
    assert not removing.done()
    release.set()
    await saving
    await removing
    path = tmp_path / ".storage/ezviz_cloud.entry.token"
    assert not path.exists()
    with pytest.raises(RuntimeError, match="removed"):
        await asyncio.to_thread(persistence.save, credentials())
    assert not path.exists()
    await EzvizTokenStore.async_remove(hass, entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "rf_session_id", "api_url", "username", "user_id", "feature_code", "push_profile"])
async def test_incomplete_initial_token_requests_reauth_before_sdk_construction(tmp_path, monkeypatch, field):
    hass = HomeAssistant(str(tmp_path))
    token = credentials()
    token.pop(field)
    entry: Any = SimpleNamespace(entry_id="entry", data={"type": "EZVIZ_CLOUD_ACCOUNT", CONF_TOKEN: token}, options={})
    client = Mock()
    monkeypatch.setattr(integration, "EzvizClient", client)
    with pytest.raises(ConfigEntryAuthFailed):
        await integration.async_setup_entry(hass, entry)
    client.assert_not_called()


def runtime_entry(hass):
    """Use HA's real entry task ownership, without registering devices."""
    hass.config_entries = Mock(async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock())
    return ConfigEntry(
        domain="ezviz_cloud", entry_id="lifecycle", title="EZVIZ test", unique_id="test",
        data={"type": "EZVIZ_CLOUD_ACCOUNT", CONF_TOKEN: credentials()},
        options={}, version=4, minor_version=1, source="user",
        discovery_keys=MappingProxyType({}), subentries_data=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["login", "refresh", "platforms"])
async def test_setup_failure_closes_client_and_leaves_no_runtime(tmp_path, monkeypatch, failure_stage):
    hass = HomeAssistant(str(tmp_path))
    entry = runtime_entry(hass)
    client = Mock()
    coordinator = Mock(async_config_entry_first_refresh=AsyncMock(), async_shutdown=AsyncMock())
    persistence = Mock(async_load=AsyncMock(return_value=credentials()))
    monkeypatch.setattr(integration, "EzvizClient", Mock(return_value=client))
    monkeypatch.setattr(integration, "EzvizTokenStore", Mock(return_value=persistence))
    monkeypatch.setattr(integration, "EzvizDataUpdateCoordinator", Mock(return_value=coordinator))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    hass.http = Mock()
    monkeypatch.setattr(integration, "ImageProxyView", Mock())
    error = RuntimeError("synthetic setup failure")
    if failure_stage == "login":
        client.login.side_effect = error
    elif failure_stage == "refresh":
        coordinator.async_config_entry_first_refresh.side_effect = error
    else:
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await integration.async_setup_entry(hass, entry)
    client.close_session.assert_called_once()
    if failure_stage != "login":
        coordinator.async_shutdown.assert_awaited_once()
    assert not hasattr(entry, "runtime_data")
    client.get_mqtt_client.assert_not_called()
    assert not hass._shutdown_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_monitor_first, stop_timeout", [(False, False), (True, False), (False, True)])
async def test_real_ha_shutdown_cleans_up_even_after_monitor_cancellation(tmp_path, monkeypatch, cancel_monitor_first, stop_timeout):
    hass = HomeAssistant(str(tmp_path))
    entry = runtime_entry(hass)
    client, mqtt = Mock(), Mock()
    client.get_mqtt_client.return_value = mqtt
    coordinator = Mock(async_config_entry_first_refresh=AsyncMock(), async_shutdown=AsyncMock())
    monkeypatch.setattr(integration, "EzvizClient", Mock(return_value=client))
    monkeypatch.setattr(integration, "EzvizTokenStore", Mock(return_value=Mock(
        async_load=AsyncMock(return_value=credentials())
    )))
    monkeypatch.setattr(integration, "EzvizDataUpdateCoordinator", Mock(return_value=coordinator))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    hass.http = Mock()
    monkeypatch.setattr(integration, "ImageProxyView", Mock())
    hass.set_state(CoreState.running)
    assert await integration.async_setup_entry(hass, entry)
    handler = entry.runtime_data.push
    async with asyncio.timeout(1):
        while not mqtt.connect.called:
            await asyncio.sleep(0)
    if cancel_monitor_first:
        handler._task.cancel()
        await asyncio.gather(handler._task, return_exceptions=True)
    released = Event()
    if stop_timeout:
        monkeypatch.setattr(integration.mqtt, "PUSH_STOP_TIMEOUT_SECONDS", 0.01)
        def blocked_stop():
            assert released.wait(2), "HA did not reach its stop phase"
        mqtt.stop.side_effect = blocked_stop
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _event: released.set())
    try:
        await hass.async_stop()
    finally:
        released.set()
    mqtt.stop.assert_called_once()
    if not stop_timeout:
        client.close_session.assert_called_once()
    coordinator.async_shutdown.assert_awaited_once()
    assert handler._mqtt is None
    assert handler._stop_task.done()
    assert hass.state is CoreState.stopped


@pytest.mark.asyncio
async def test_unload_failure_restores_push_then_success_releases_runtime(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    entry = runtime_entry(hass)
    client = Mock()
    coordinator = Mock(async_shutdown=AsyncMock())
    old = Mock(async_stop=AsyncMock(return_value=True))
    entry.runtime_data = EzvizRuntimeData(client, coordinator, old, Mock())
    new = Mock(async_stop=AsyncMock(return_value=True))
    monkeypatch.setattr(integration, "EzvizMqttHandler", Mock(return_value=new))
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(side_effect=[False, True]))
    assert not await integration.async_unload_entry(hass, entry)
    assert entry.runtime_data.push is new
    new.async_start.assert_called_once()
    client.close_session.assert_not_called()
    assert await integration.async_unload_entry(hass, entry)
    assert not hasattr(entry, "runtime_data")
    client.close_session.assert_called_once()
    coordinator.async_shutdown.assert_awaited_once()
