"""SDK lock contention and sticky fatal workers through actual HA boundaries."""

import asyncio
import json
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import MagicMock

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizPushFatalError,
    EzvizTokenPersistenceError,
)
from pyezvizapi.push import mqtt as sdk_mqtt
from pyezvizapi.push.longlink import auth as push_auth
from pyezvizapi.push.longlink.codec import AuthenticationRejected
from pyezvizapi.push.longlink.worker import PushWorker
import pytest
from test_hardening import entities
from test_token_identity import identity_token

from custom_components.ezviz_cloud import diagnostics, mqtt, tokens
from homeassistant.config_entries import ConfigEntry


async def test_diagnostics_lock_allows_real_durable_callback_to_finish(hass, monkeypatch):
    entry, _, data, _, _, _ = entities(hass)
    owner = tokens.TokenPersistence(hass, entry)
    token = identity_token(service_urls={"pushDasDomain": "push.example.invalid"})
    api = EzvizClient(token=token, on_token_updated=owner.save_from_worker)
    sdk = api.get_mqtt_client()
    worker = PushWorker(lambda: None)
    sdk._push_worker = worker
    handler = mqtt.EzvizMqttHandler(hass, api, entry.entry_id)
    handler._mqtt = sdk
    entry.runtime_data = SimpleNamespace(coordinator=data, push=handler)
    held, requested = Event(), Event()
    threads = []
    original = handler.get_diagnostics

    def diagnostic():
        threads.append(get_ident())
        requested.set()
        return original()

    monkeypatch.setattr(handler, "get_diagnostics", diagnostic)
    # Bound the negative control too: old code blocks HA until this expires.
    monkeypatch.setattr(tokens, "PERSIST_TIMEOUT", 0.5)

    def save_while_locked():
        with worker._lock:
            held.set()
            assert requested.wait(2)
            owner.save_from_worker(token)

    writer = hass.async_add_executor_job(save_while_locked)
    try:
        assert await asyncio.to_thread(held.wait, 2)
        async with asyncio.timeout(2):
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
            await writer
        assert len(threads) == 1 and threads[0] != get_ident()
        assert result["push"]["state"] == "idle"
        assert json.loads(Path(owner._store.path).read_text())["data"]["token"] == token
    finally:
        requested.set()
        await asyncio.gather(writer, return_exceptions=True)
        await owner.async_drain()
        sdk.stop()
        api.close_session()
        await data.async_close()


@pytest.mark.parametrize("phase", ["startup", "health"])
@pytest.mark.parametrize("failure", ["registration", "refresh", "longlink", "storage", "storage_auth_cause", "other"])
async def test_real_sdk_cached_fatal_stops_retrying(hass, monkeypatch, phase, failure, caplog):  # noqa: PLR0915
    entry, _, data, _, _, _ = entities(hass)
    token = identity_token(
        rf_session_id="" if failure == "registration" else "refresh",
        service_urls={"pushDasDomain": "push.example.invalid"},
    )
    api = EzvizClient(token=token, on_token_updated=lambda _token: None)
    sdk = api.get_mqtt_client()
    assert api.get_mqtt_client() is sdk  # The SDK cache, not a fake client factory.
    response = SimpleNamespace(status_code=401)
    session = SimpleNamespace(headers={}, put=lambda *a, **k: response)
    monkeypatch.setattr(sdk_mqtt, "isolated_session", lambda _session: session)

    def refresh(*_args):
        raise EzvizAuthTokenExpired("synthetic private auth detail")

    monkeypatch.setattr(sdk_mqtt, "refresh_credentials", refresh)

    def reject_handshake(*_args):
        raise AuthenticationRejected(3)

    monkeypatch.setattr(push_auth, "_response", lambda *_args: b"")
    monkeypatch.setattr(push_auth.wire, "authentication_ii", reject_handshake)

    def run(_stopped):
        if failure in ("registration", "refresh"):
            sdk._prepare_channel99(token, lambda _snapshot: None)
        elif failure == "longlink":
            push_auth.authenticate(None, b"SYNTHETIC", "session", {}, lambda _state: None)
        elif failure == "storage_auth_cause":
            raise EzvizTokenPersistenceError("private disk detail") from EzvizAuthTokenExpired()
        elif failure == "storage":
            raise EzvizTokenPersistenceError("private disk detail")
        else:
            raise EzvizPushFatalError("private unknown failure")

    worker = PushWorker(lambda: SimpleNamespace(run=run, close=lambda: None))
    sdk._push_worker = worker
    if phase == "startup":
        # A previous worker has already failed; connect must raise that same error.
        await hass.async_add_executor_job(worker._run)
    connect = MagicMock(wraps=sdk.connect)
    monkeypatch.setattr(sdk, "connect", connect)
    reauth = MagicMock()
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", reauth)
    monkeypatch.setattr(mqtt, "PUSH_HEALTH_SECONDS", 0.001)
    monkeypatch.setattr(mqtt, "PUSH_RETRY_SECONDS", 0.001)
    handler = mqtt.EzvizMqttHandler(hass, api, entry.entry_id)
    entry.runtime_data = SimpleNamespace(coordinator=data, push=handler)
    handler.async_start()
    try:
        async with asyncio.timeout(2):
            while handler.get_diagnostics()["state"] != "fatal":
                await asyncio.sleep(0.001)
            if handler._health_task:
                await handler._health_task
            await handler._task
        await asyncio.sleep(0.025)  # Several shortened retry intervals.
        assert connect.call_count == 1
        assert reauth.call_count == (1 if failure in ("registration", "refresh", "longlink") else 0)
        assert worker.failure is not None
        # Failure remains sticky even after stop; HA must not reuse it blindly.
        with pytest.raises(EzvizPushFatalError):
            await hass.async_add_executor_job(sdk.connect)
        assert api.get_mqtt_client() is sdk
        assert handler.get_diagnostics()["state"] == "fatal"
        assert handler.get_diagnostics()["fatal_failure_category"] == (
            "storage" if failure.startswith("storage") else "other" if failure == "other" else "authentication"
        )
        assert data.last_update_success
        assert "private disk detail" not in caplog.text
        assert "private unknown failure" not in caplog.text
        assert "synthetic private auth detail" not in caplog.text
        assert "Automatic push retries paused" in caplog.text
        if failure.startswith("storage"):
            assert "storage permissions and free space" in caplog.text
        # Repeated detection cannot create another reauth prompt.
        await handler._async_handle_terminal_failure(worker.failure)
        assert reauth.call_count == (1 if failure in ("registration", "refresh", "longlink") else 0)
    finally:
        await handler.async_wait_stopped()
        api.close_session()
        await data.async_close()
