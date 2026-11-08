"""Real HA setup/config flow/token migration, without any production login."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi import DeviceSnapshot, EzvizClient
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import config_flow, coordinator, mqtt
from custom_components.ezviz_cloud.const import (
    ATTR_TYPE_CLOUD,
    CONF_REGION,
    CONF_SDK_TOKEN,
    DOMAIN,
    REGION_EU,
)
from custom_components.ezviz_cloud.tokens import TokenPersistence
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import CONF_PASSWORD, CONF_TIMEOUT, CONF_TYPE, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed


async def test_actual_config_flow_uses_credential_profile_and_full_token(hass, monkeypatch):
    token = {
        "session_id": "new",
        "rf_session_id": "refresh",
        "username": "internal-user",
        "push_profile": "android",
        "push_state": {"counter": 2},
        "service_urls": {"push": "synthetic"},
    }
    api = SimpleNamespace(
        enable_channel99=MagicMock(return_value=token), login=MagicMock(), close_session=MagicMock()
    )
    monkeypatch.setattr(config_flow, "EzvizClient", MagicMock(return_value=api))
    flow = config_flow.EzvizConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = DOMAIN
    result = await flow.async_step_user(
        {
            CONF_USERNAME: "synthetic@example.invalid",
            CONF_PASSWORD: "not-stored",
            CONF_REGION: REGION_EU,
            CONF_TIMEOUT: 20,
        }
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SDK_TOKEN] == token
    assert CONF_PASSWORD not in result["data"]
    assert result["data"][CONF_SDK_TOKEN] is not token
    api.enable_channel99.assert_called_once_with()
    api.login.assert_not_called()
    api.close_session.assert_called_once_with()


async def test_setup_restores_full_token_installs_callback_and_closes_after_unload(
    hass, monkeypatch
):
    full = {
        "session_id": "synthetic",
        "rf_session_id": "refresh",
        "username": "internal-user",
        "api_url": "api.example.invalid",
        "push_profile": "synthetic-profile",
        "push_state": {"counter": 7},
    }
    observed = {}
    real_type = EzvizClient

    def build(*, token, timeout, on_token_updated):
        observed["token"] = deepcopy(token)
        observed["callback"] = on_token_updated
        api = real_type(token=token, timeout=timeout, on_token_updated=on_token_updated)
        monkeypatch.setattr(api, "login", api.export_token)
        monkeypatch.setattr(api, "get_device_records", lambda _: {})
        observed["close"] = MagicMock(wraps=api.close_session)
        monkeypatch.setattr(api, "close_session", observed["close"])
        return api

    assert "on_token_updated" in signature(build).parameters
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", build)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True))
    monkeypatch.setattr(hass, "http", MagicMock(), raising=False)
    monkeypatch.setattr(mqtt.EzvizMqttHandler, "async_start", MagicMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: ATTR_TYPE_CLOUD,
            "session_id": "synthetic",
            "rf_session_id": "refresh",
            "user_id": "internal-user",
            "url": "api.example.invalid",
            CONF_SDK_TOKEN: full,
        },
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    context = current_entry.set(entry)
    try:
        assert await ezviz_cloud.async_setup_entry(hass, entry)
    finally:
        current_entry.reset(context)
    assert observed["token"] == full
    changed = {**full, "push_state": {"counter": 8}}
    await hass.async_add_executor_job(observed["callback"], changed)
    assert await TokenPersistence(hass, entry).async_load() == changed
    # Setup + callback must not rewrite config-entry anchor fields on each ACK.
    assert entry.data[CONF_SDK_TOKEN] == full
    assert await ezviz_cloud.async_unload_entry(hass, entry)
    observed["close"].assert_called_once_with()
    await ezviz_cloud.async_remove_entry(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        await TokenPersistence(hass, entry).async_load() == full
    )  # no store, fallback to removed entry data only
    assert not Path(TokenPersistence(hass, entry)._store.path).exists()


async def test_poll_timeout_and_close_keep_single_worker_and_reject_late_publish(hass, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def executor(func, *args):
        calls.append(func)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return DeviceSnapshot(1, datetime.now(UTC), {}, {})
        return func(*args)

    api = SimpleNamespace(poll_devices=lambda options: None, close_session=MagicMock())
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    context = current_entry.set(entry)
    try:
        data = coordinator.EzvizDataUpdateCoordinator(hass, config_entry=entry, api=api, api_timeout=0.01)
    finally:
        current_entry.reset(context)
    monkeypatch.setattr(hass, "async_add_executor_job", executor)
    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await data._async_update_data()
    assert len(calls) == 1
    close = asyncio.create_task(data.async_close())
    await asyncio.sleep(0)
    api.close_session.assert_not_called()
    release.set()
    await close
    api.close_session.assert_called_once_with()
    with pytest.raises(UpdateFailed, match="shutting down"):
        await data._async_update_data()
