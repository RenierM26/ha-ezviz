"""Integration boundaries exercised against an installed Home Assistant runtime."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from pyezvizapi.exceptions import EzvizAuthVerificationCode

from custom_components.ezviz_cloud import config_flow
from custom_components.ezviz_cloud.const import CONF_TOKEN
from custom_components.ezviz_cloud.token_store import EzvizTokenStore
from homeassistant.core import CoreState, HomeAssistant
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
