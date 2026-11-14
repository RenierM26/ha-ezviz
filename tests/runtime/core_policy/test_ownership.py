"""Real HA entry lifetime and private Store deletion ownership regressions."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import runtime, tokens
from custom_components.ezviz_cloud.const import ATTR_TYPE_CLOUD, CONF_AUTH_REF, DOMAIN
from pyezvizapi import DeviceSnapshot
import pytest

from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

from tests.runtime import test_config_flow as controls
from tests.runtime.test_hardening import entities

TOKEN = {
    "session_id": "synthetic",
    "rf_session_id": "refresh",
    "username": "user",
    "api_url": "api.example.invalid",
    "feature_code": "a" * 32,
}


@pytest.fixture(autouse=True)
def integration(hass: HomeAssistant) -> None:
    """Resolve the real integration for HA's setup/removal dispatch."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component


@pytest.mark.parametrize("failure_at", ["view", "push_start"])
async def test_setup_failure_closes_owned_client_and_allows_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    """Failures after the first poll cannot strand an owner and block all retries."""
    entry = controls.add_entry(
        hass, version=5, data={"type": ATTR_TYPE_CLOUD, "sdk_token": TOKEN}
    )
    clients = []

    def client(**kwargs: object) -> SimpleNamespace:
        api = SimpleNamespace(
            login=MagicMock(),
            export_token=lambda: dict(TOKEN),
            poll_devices=lambda _options: DeviceSnapshot(1, datetime.now(UTC), {}, {}),
            close_session=MagicMock(),
        )
        clients.append(api)
        return api

    monkeypatch.setattr(ezviz_cloud, "EzvizClient", client)
    start = MagicMock(
        side_effect=RuntimeError("synthetic setup failure")
        if failure_at == "push_start"
        else None
    )
    push = SimpleNamespace(async_start=start, async_wait_stopped=AsyncMock())
    monkeypatch.setattr(ezviz_cloud, "EzvizMqttHandler", lambda *_args: push)
    monkeypatch.setattr(ezviz_cloud, "ImageProxyView", lambda _hass: object())
    register = MagicMock(
        side_effect=RuntimeError("synthetic setup failure")
        if failure_at == "view"
        else None
    )
    hass.http = SimpleNamespace(register_view=register)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    async with entry.setup_lock:
        await entry.async_setup(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    owner = hass.data[DOMAIN]["_runtimes"][entry.entry_id]
    try:
        clients[0].close_session.assert_called_once()
        assert owner.cleanup_complete
        register.side_effect = None
        start.side_effect = None
        async with entry.setup_lock:
            await entry.async_setup(hass)
        assert entry.state is ConfigEntryState.LOADED
        assert len(clients) == 2
        assert entry.runtime_data is not owner
        assert owner.cleanup_complete
    finally:
        await owner.async_wait_stopped()
        if hasattr(entry, "runtime_data"):
            await entry.runtime_data.async_wait_stopped()


@pytest.mark.parametrize("phase", ["token", "pending", "identity"])
async def test_real_entry_removal_retries_private_file_failure(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient unlink failure must not abandon removed-account private files."""
    entry = controls.add_entry(hass, version=5)
    persistence = tokens.TokenPersistence(hass, entry)
    await persistence.async_save(TOKEN)
    await persistence.async_identity(TOKEN)
    reference = await tokens.stage_login(hass, entry.unique_id, TOKEN)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_AUTH_REF: reference}
    )
    identity = tokens._identity_state(hass, persistence._account)
    paths = {
        "token": Path(persistence._store.path),
        "pending": Path(tokens._pending_store(hass, reference).path),
        "identity": Path(identity.store.path),
    }
    original = storage.os.unlink
    failures = []

    def unlink(path: str) -> None:
        if Path(path) == paths[phase] and len(failures) < 2:
            failures.append(path)
            raise PermissionError("SECRET_MARKER")
        original(path)

    monkeypatch.setattr(storage.os, "unlink", unlink)
    monkeypatch.setattr(ezviz_cloud, "REMOVE_RETRY_SECONDS", 0.001, raising=False)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert len(failures) == 2
    assert "SECRET_MARKER" not in caplog.text
    assert caplog.text.count("credential removal incomplete") == 1
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert all(not path.exists() for path in paths.values())
    with pytest.raises(OSError, match="removed"):
        await persistence.async_save(TOKEN)


async def test_old_removal_preserves_successor_account_identity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new entry can own the shared account identity before old cleanup finishes."""
    old = controls.add_entry(hass, version=5)
    persistence = tokens.TokenPersistence(hass, old)
    await persistence.async_save(TOKEN)
    identity = await persistence.async_identity(TOKEN)
    identity_path = Path(tokens._identity_state(hass, persistence._account).store.path)
    entered, release = Event(), Event()
    original = storage.os.unlink

    def unlink(path: str) -> None:
        if Path(path) == Path(persistence._store.path):
            entered.set()
            assert release.wait(5)
        original(path)

    monkeypatch.setattr(storage.os, "unlink", unlink)
    try:
        await hass.config_entries.async_remove(old.entry_id)
        assert await asyncio.to_thread(entered.wait, 2)
        successor = controls.add_entry(hass, version=5)
        owner = tokens.TokenPersistence(hass, successor)
        await owner.async_save(TOKEN)
        assert await owner.async_identity(TOKEN) == identity
    finally:
        release.set()
        await hass.async_block_till_done()
    assert identity_path.exists()
    assert await owner.async_load() == TOKEN
    assert hass.config_entries.async_get_entry(successor.entry_id) is successor


async def test_failed_platform_unload_preserves_working_owner(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HA unload must keep a working owner when a platform refuses unload."""
    entry, api, data, *_ = entities(hass)
    owner = runtime.RuntimeCleanup(hass, data)
    entry.runtime_data = owner
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    unload = AsyncMock(return_value=False)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    try:
        async with entry.setup_lock:
            assert not await entry.async_unload(hass)
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert entry.runtime_data is owner
        assert not data.shutting_down
        assert await data.async_execute(lambda: "working") == "working"
        api.close_session.assert_not_called()
        # HA marks FAILED_UNLOAD unrecoverable; do not invent a retry transition.
        assert not entry.state.recoverable
    finally:
        await owner.async_wait_stopped()


async def test_private_removal_retry_remains_owned_through_ha_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA stop joins deletion retry/executor work rather than cancelling its owner."""
    entry = controls.add_entry(hass, version=5)
    persistence = tokens.TokenPersistence(hass, entry)
    await persistence.async_save(TOKEN)
    path = Path(persistence._store.path)
    entered, release = Event(), Event()
    original = storage.os.unlink
    attempts = []

    def unlink(value: str) -> None:
        if Path(value) == path:
            attempts.append(value)
            if len(attempts) == 1:
                raise PermissionError("synthetic transient deletion")
            entered.set()
            assert release.wait(5)
        original(value)

    monkeypatch.setattr(storage.os, "unlink", unlink)
    monkeypatch.setattr(ezviz_cloud, "REMOVE_RETRY_SECONDS", 0.001, raising=False)
    try:
        await hass.config_entries.async_remove(entry.entry_id)
        assert await asyncio.to_thread(entered.wait, 2)
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
    assert len(attempts) == 2
    assert not path.exists()
