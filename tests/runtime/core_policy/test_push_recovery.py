"""SDK lock contention and sticky fatal workers through actual HA boundaries."""

import asyncio
from collections.abc import AsyncGenerator
import json
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from custom_components.ezviz_cloud import diagnostics, mqtt, tokens
from custom_components.ezviz_cloud.availability import device_available
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizPushFatalError,
    EzvizTokenPersistenceError,
)
from pyezvizapi.models import EzvizDeviceRecord
from pyezvizapi.push import mqtt as sdk_mqtt
from pyezvizapi.push.longlink import auth as push_auth
from pyezvizapi.push.longlink.codec import AuthenticationRejected
from pyezvizapi.push.longlink.worker import PushWorker
import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from tests.runtime.test_hardening import entities, entry_for
from tests.runtime.test_token_identity import identity_token


async def test_diagnostics_lock_allows_real_durable_callback_to_finish(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK locks must not block the HA loop needed by a durable Store callback."""
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
    threads: list[int] = []
    original = handler.get_diagnostics

    def diagnostic() -> dict[str, Any]:
        threads.append(get_ident())
        requested.set()
        return original()

    monkeypatch.setattr(handler, "get_diagnostics", diagnostic)
    # Bound the negative control too: old code blocks HA until this expires.
    monkeypatch.setattr(tokens, "PERSIST_TIMEOUT", 0.5)

    def save_while_locked() -> None:
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
@pytest.mark.parametrize(
    "failure",
    ["registration", "refresh", "longlink", "storage", "storage_auth_cause", "other"],
)
async def test_real_sdk_cached_fatal_stops_retrying(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cached SDK fatal errors stop retries and request only justified reauth."""
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

    def refresh(*_args: object, **_kwargs: object) -> None:
        assert _kwargs == {"feature_code": token["feature_code"]}
        raise EzvizAuthTokenExpired("synthetic private auth detail")

    monkeypatch.setattr(sdk_mqtt, "refresh_credentials", refresh)

    def reject_handshake(*_args: object) -> None:
        raise AuthenticationRejected(3)

    monkeypatch.setattr(push_auth, "_response", lambda *_args: b"")
    monkeypatch.setattr(push_auth.wire, "authentication_ii", reject_handshake)

    def run(_stopped: Event) -> None:
        if failure in ("registration", "refresh"):
            sdk._prepare_channel99(token, lambda _snapshot: None)
        elif failure == "longlink":
            push_auth.authenticate(
                None, b"SYNTHETIC", "session", {}, lambda _state: None
            )
        elif failure == "storage_auth_cause":
            raise EzvizTokenPersistenceError(
                "private disk detail"
            ) from EzvizAuthTokenExpired()
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
        assert reauth.call_count == (
            1 if failure in ("registration", "refresh", "longlink") else 0
        )
        assert worker.failure is not None
        # Failure remains sticky even after stop; HA must not reuse it blindly.
        with pytest.raises(EzvizPushFatalError):
            await hass.async_add_executor_job(sdk.connect)
        assert api.get_mqtt_client() is sdk
        assert handler.get_diagnostics()["state"] == "fatal"
        assert handler.get_diagnostics()["fatal_failure_category"] == (
            "storage"
            if failure.startswith("storage")
            else "other"
            if failure == "other"
            else "authentication"
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
        assert reauth.call_count == (
            1 if failure in ("registration", "refresh", "longlink") else 0
        )
    finally:
        await handler.async_wait_stopped()
        api.close_session()
        await data.async_close()


class ControlledSession:
    """Synthetic session IO; SDK worker/thread/retry ownership remains real."""

    def __init__(self, mode: str, gate: Event) -> None:
        """Expose deterministic transport and shutdown barriers."""
        self.mode = mode
        self.gate = gate
        self.ready = Event()
        self.closed = Event()

    def run(self, stopped: Event) -> None:
        """Disconnect/fail once, or wait for the actual worker's stop signal."""
        if self.mode == "transport":
            raise OSError("PRIVATE_TRANSPORT_DETAIL")
        if self.mode == "timeout":
            raise TimeoutError("PRIVATE_TRANSPORT_DETAIL")
        if self.mode == "disconnect":
            return
        assert self.gate.wait(3)
        self.ready.set()
        assert stopped.wait(5)

    def close(self) -> None:
        """Record the SDK's session cleanup."""
        self.closed.set()

    def diagnostics(self) -> dict[str, Any]:
        """Keep ready status observable without making it live broker proof."""
        return {"ready": self.ready.is_set(), "private": "PRIVATE_SESSION_DETAIL"}


type PushContext = tuple[
    EzvizClient, EzvizDataUpdateCoordinator, RuntimeCleanup, mqtt.EzvizMqttHandler
]


@pytest.fixture
async def actual_push(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[PushContext]:
    """Real SDK client/polling, real HA runtime, only account/session IO synthetic."""
    entry = entry_for(hass)
    api = EzvizClient(
        token=identity_token(service_urls={"pushDasDomain": "push.example.invalid"}),
        on_token_updated=lambda _token: None,
    )
    record = EzvizDeviceRecord.from_api(
        "CAM001",
        {
            "deviceInfos": {
                "deviceSerial": "CAM001",
                "deviceCategory": "IPC",
                "deviceSubCategory": "CP8",
                "name": "Synthetic",
                "version": "1",
                "status": 1,
                "supportExt": {},
            },
            "WIFI": {},
            "STATUS": {},
            "SWITCH": {},
        },
    )
    monkeypatch.setattr(
        api, "get_device_records", lambda _categories: {"CAM001": record}
    )
    monkeypatch.setattr(
        api, "get_device_messages_list", lambda **_kwargs: {"messages": []}
    )
    coordinator = EzvizDataUpdateCoordinator(
        hass, config_entry=entry, api=api, api_timeout=2
    )
    handler = mqtt.EzvizMqttHandler(hass, api, entry.entry_id)
    runtime = RuntimeCleanup(hass, coordinator, handler)
    entry.runtime_data = runtime
    monkeypatch.setattr(mqtt, "PUSH_HEALTH_SECONDS", 0.001)
    try:
        yield api, coordinator, runtime, handler
    finally:
        await runtime.async_wait_stopped()


@pytest.mark.parametrize("failure", ["transport", "timeout", "disconnect"])
async def test_real_worker_recovers_without_restarting_ha_polling(
    hass: HomeAssistant,
    actual_push: PushContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """SDK session retry is independent of HA polling, reauth and client startup."""
    api, coordinator, _, handler = actual_push
    sdk = api.get_mqtt_client()
    gate = Event()
    first, restored = (
        ControlledSession(failure, gate),
        ControlledSession("healthy", gate),
    )
    sessions: list[ControlledSession] = []

    def new_session() -> ControlledSession:
        session = first if not sessions else restored
        sessions.append(session)
        return session

    worker = PushWorker(new_session, retry_delay=0.01)
    sdk._push_worker = worker
    connect = MagicMock(wraps=sdk.connect)
    health = MagicMock(wraps=sdk.raise_if_failed)
    reauth = MagicMock()
    monkeypatch.setattr(sdk, "connect", connect)
    monkeypatch.setattr(sdk, "raise_if_failed", health)
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", reauth)
    try:
        handler.async_start()
        assert handler._task is not None
        await asyncio.wait_for(asyncio.shield(handler._task), 2)
        assert await asyncio.to_thread(first.closed.wait, 2)
        await coordinator.async_refresh()  # Real SDK poll while recovery is gated.
        assert coordinator.last_update_success
        assert device_available(coordinator, "CAM001")
        assert not restored.ready.is_set()
        gate.set()
        assert await asyncio.to_thread(restored.ready.wait, 2)
        async with asyncio.timeout(2):
            while health.call_count < 2:
                await asyncio.sleep(0.001)
        state = await hass.async_add_executor_job(handler.get_diagnostics)
        assert state["state"] == "connected"
        assert state["attempts"] == 2
        assert state["transient_failures"] == (0 if failure == "disconnect" else 1)
        assert worker.failure is None
        assert api.get_mqtt_client() is sdk
        assert connect.call_count == 1
        reauth.assert_not_called()
        assert "PRIVATE_" not in json.dumps(state)
        assert "PRIVATE_" not in caplog.text
        assert len(sessions) == 2
    finally:
        gate.set()
        await handler.async_wait_stopped()
    assert first.closed.is_set() and restored.closed.is_set()
    assert not worker.diagnostics()["worker_alive"]


async def test_real_worker_retry_wait_is_interrupted_before_http_close(
    hass: HomeAssistant, actual_push: PushContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime shutdown interrupts a real 60-second retry wait within two seconds."""
    api, coordinator, runtime, handler = actual_push
    sdk = api.get_mqtt_client()
    first = ControlledSession("transport", Event())
    factory = MagicMock(return_value=first)
    worker = PushWorker(factory, retry_delay=60)
    sdk._push_worker = worker
    original_close = api.close_session
    closed = Event()

    def close_http() -> None:
        assert not worker.diagnostics()["worker_alive"]
        original_close()
        closed.set()

    monkeypatch.setattr(api, "close_session", close_http)
    handler.async_start()
    assert handler._task is not None
    await asyncio.wait_for(asyncio.shield(handler._task), 2)
    assert await asyncio.to_thread(first.closed.wait, 2)
    state = await hass.async_add_executor_job(handler.get_diagnostics)
    assert state["state"] == "retry_wait"
    await coordinator.async_refresh()
    assert device_available(coordinator, "CAM001")
    async with asyncio.timeout(2):
        await runtime.async_wait_stopped()
    assert runtime.cleanup_complete and closed.is_set()
    assert not device_available(coordinator, "CAM001")
    assert not worker.diagnostics()["worker_alive"]
    factory.assert_called_once()
    previous = handler._task
    handler.async_start()
    assert handler._task is previous
