"""Offline regressions for optional push using a minimal HA boundary."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.exceptions import EzvizPushFatalError, EzvizTokenPersistenceError, HTTPError
import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_cloud"


@pytest.fixture
def integration(monkeypatch):
    """Load source with mocked HA dependencies, without a HA installation."""
    package = ModuleType("push_test")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "push_test", package)
    for name in (
        "homeassistant", "homeassistant.config_entries", "homeassistant.const",
        "homeassistant.core", "homeassistant.exceptions", "homeassistant.helpers",
        "push_test.const", "push_test.coordinator", "push_test.views", "push_test.token_store",
    ):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    constants = sys.modules["push_test.const"]
    for name in (
        "DOMAIN", "DATA_COORDINATOR", "MQTT_HANDLER", "ATTR_TYPE_CLOUD",
        "CONF_SESSION_ID", "CONF_RF_SESSION_ID", "CONF_USER_ID", "CONF_TOKEN",
    ):
        setattr(constants, name, name.lower())
    for name in ("CONF_TYPE", "CONF_URL", "CONF_TIMEOUT"):
        setattr(sys.modules["homeassistant.const"], name, name.lower())

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, ROOT / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    mqtt = load("push_test.mqtt", "mqtt.py")
    setup = load("push_test", "__init__.py")
    return SimpleNamespace(mqtt=mqtt, setup=setup)


def make_handler(integration):
    mqtt, client = MagicMock(), MagicMock()
    client.get_mqtt_client.return_value = mqtt

    async def executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        data={"domain": {"entry": {"data_coordinator": MagicMock()}}},
        async_add_executor_job=executor,
        async_create_background_task=lambda coro, _name: asyncio.create_task(coro),
    )
    return integration.mqtt.EzvizMqttHandler(hass, client, SimpleNamespace(entry_id="entry", async_start_reauth=MagicMock())), mqtt, hass


@pytest.mark.parametrize("error", [HTTPError(), OSError(), TimeoutError(), ValueError()])
def test_failure_and_cleanup_error_do_not_escape_and_retry_recovers(integration, monkeypatch, error):
    async def scenario():
        handler, mqtt, _ = make_handler(integration)
        mqtt.connect.side_effect = [error, None]
        mqtt.stop.side_effect = [HTTPError(), None, None]
        monkeypatch.setattr(integration.mqtt, "PUSH_RETRY_SECONDS", 0.001)
        handler.async_start()
        async with asyncio.timeout(1):
            while mqtt.connect.call_count < 2:
                await asyncio.sleep(0.001)
        assert mqtt.connect.call_count == 2
        assert mqtt.stop.call_count == 2
        await handler.async_stop()

    asyncio.run(scenario())


def test_unload_interrupts_retry_delay(integration):
    async def scenario():
        handler, mqtt, _ = make_handler(integration)
        mqtt.connect.side_effect = HTTPError()
        handler.async_start()
        await asyncio.sleep(0)
        await asyncio.wait_for(handler.async_stop(), 1)
        assert mqtt.connect.call_count == 1
        assert handler._task.done()
        handler.async_start()
        assert mqtt.connect.call_count == 1

    asyncio.run(scenario())


def test_stop_waits_for_inflight_start_then_disconnects(integration):
    async def scenario():
        handler, mqtt, hass = make_handler(integration)
        entered, release = asyncio.Event(), asyncio.Event()

        async def executor(func, *args):
            if func == handler.start:
                entered.set()
                await release.wait()
            return func(*args)

        hass.async_add_executor_job = executor
        handler.async_start()
        await entered.wait()
        stop_task = asyncio.create_task(handler.async_stop())
        await asyncio.sleep(0)
        assert not stop_task.done()
        mqtt.stop.assert_not_called()
        release.set()
        await stop_task
        mqtt.connect.assert_called_once()
        mqtt.stop.assert_called_once()

    asyncio.run(scenario())


def test_coordinator_ready_before_connect_and_duplicate_start_ignored(integration):
    async def scenario():
        handler, mqtt, hass = make_handler(integration)

        def connect():
            assert handler._coordinator is hass.data["domain"]["entry"]["data_coordinator"]

        mqtt.connect.side_effect = connect
        handler.async_start()
        handler.async_start()
        await asyncio.sleep(0)
        mqtt.connect.assert_called_once()
        await handler.async_stop()

    asyncio.run(scenario())


def test_setup_loads_entities_while_push_connect_pending(integration, monkeypatch):
    async def scenario():
        setup = integration.setup
        token = {"conf_session_id": "session", "conf_rf_session_id": "refresh"}
        client = MagicMock()
        client.login.return_value = token
        monkeypatch.setattr(setup, "EzvizClient", MagicMock(return_value=client))
        monkeypatch.setattr(setup, "EzvizTokenStore", MagicMock(return_value=SimpleNamespace(
            async_load=AsyncMock(return_value=token), save=MagicMock()
        )))
        coordinator = SimpleNamespace(async_config_entry_first_refresh=AsyncMock())
        monkeypatch.setattr(setup, "EzvizDataUpdateCoordinator", MagicMock(return_value=coordinator))
        entered, release = asyncio.Event(), asyncio.Event()

        async def executor(func, *args):
            if getattr(func, "__name__", None) == "start":
                entered.set()
                await release.wait()
            return func(*args)

        hass = SimpleNamespace(
            data={}, async_add_executor_job=executor,
            async_create_background_task=lambda coro, _name: asyncio.create_task(coro),
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
            bus=MagicMock(), http=MagicMock(),
        )
        entry = MagicMock()
        entry.entry_id = "entry"
        entry.data = {"conf_token": token, **token, "conf_type": "attr_type_cloud", "conf_url": "api.test", "conf_user_id": "user"}
        entry.options = {}
        assert await setup.async_setup_entry(hass, entry)
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        await entered.wait()
        handler = hass.data["domain"]["entry"]["mqtt_handler"]
        assert not handler._task.done()
        release.set()
        await handler.async_stop()

    asyncio.run(scenario())


def test_client_creation_failure_is_optional(integration):
    async def scenario():
        handler, mqtt, _ = make_handler(integration)
        handler._client.get_mqtt_client.side_effect = KeyError("pushAddr")
        handler.async_start()
        await asyncio.sleep(0)
        await asyncio.wait_for(handler.async_stop(), 1)
        mqtt.connect.assert_not_called()
        mqtt.stop.assert_not_called()

    asyncio.run(scenario())


def test_unload_waits_for_cleanup_before_allowing_replacement(integration, monkeypatch):
    async def scenario():
        handler, mqtt, hass = make_handler(integration)
        monkeypatch.setattr(integration.mqtt, "PUSH_STOP_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(integration.mqtt, "PUSH_RETRY_SECONDS", 0.02)
        hass.data["domain"]["entry"]["mqtt_handler"] = handler
        hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        entry = SimpleNamespace(entry_id="entry")
        handler.async_start()
        await asyncio.sleep(0)
        mqtt.stop.side_effect = HTTPError()
        assert not await integration.setup.async_unload_entry(hass, entry)
        hass.config_entries.async_unload_platforms.assert_not_awaited()
        assert "entry" in hass.data["domain"]
        assert handler._mqtt is mqtt
        mqtt.stop.side_effect = None
        await asyncio.wait_for(handler._stop_task, 2)
        assert handler._mqtt is None

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["get_client", "connect", "stop"])
@pytest.mark.parametrize("cancel_background", [False, True])
def test_shutdown_is_bounded_and_worker_eventually_cleans_up(  # noqa: PLR0915
    integration, monkeypatch, stage, cancel_background
):
    """A real blocked executor must not hold unload or lose late cleanup."""
    async def scenario():
        handler, mqtt, hass = make_handler(integration)
        entered, release, cleaned = Event(), Event(), Event()
        hass.async_add_executor_job = asyncio.to_thread
        monkeypatch.setattr(integration.mqtt, "PUSH_STOP_TIMEOUT_SECONDS", 0.01)

        def stall():
            entered.set()
            assert release.wait(5), "test did not release SDK worker"

        def get_client(**kwargs):
            if stage == "get_client":
                stall()
            return mqtt

        def connect():
            if stage == "connect":
                stall()

        def stop():
            if stage == "stop":
                stall()
            cleaned.set()

        handler._client.get_mqtt_client.side_effect = get_client
        mqtt.connect.side_effect = connect
        mqtt.stop.side_effect = stop
        hass.data["domain"]["entry"]["mqtt_handler"] = handler
        hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        entry = SimpleNamespace(entry_id="entry")
        handler.async_start()
        try:
            if stage == "stop":
                async with asyncio.timeout(1):
                    while not mqtt.connect.called:
                        await asyncio.sleep(0.001)
            else:
                assert await asyncio.to_thread(entered.wait, 1)
            assert not await asyncio.wait_for(integration.setup.async_unload_entry(hass, entry), 1)
            assert entered.is_set()
            assert not cleaned.is_set()
            assert not handler._stop_task.done()
            hass.config_entries.async_unload_platforms.assert_not_awaited()
            assert "entry" in hass.data["domain"]
            if cancel_background:
                handler._stop_task.cancel()
                handler._task.cancel()
                await asyncio.gather(handler._stop_task, handler._task, return_exceptions=True)
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 1)
        if not cancel_background:
            await asyncio.wait_for(handler._stop_task, 2)
        mqtt.stop.assert_called_once()
        mqtt.connect.assert_called_once()

    asyncio.run(scenario())


def test_failed_cleanup_retains_client_and_prevents_replacement(integration):
    """Repeated failed cleanup must not accumulate untracked Paho clients."""
    handler, mqtt, _ = make_handler(integration)
    mqtt.connect.side_effect = HTTPError()
    mqtt.stop.side_effect = HTTPError()
    with pytest.raises(HTTPError):
        handler.start()
    for _ in range(3):
        assert not handler.stop()
        assert handler._mqtt is mqtt
        with pytest.raises(RuntimeError, match="cleanup is still pending"):
            handler.start()
    handler._client.get_mqtt_client.assert_called_once()
    mqtt.connect.assert_called_once()

    replacement = MagicMock()
    handler._client.get_mqtt_client.return_value = replacement
    mqtt.stop.side_effect = None
    handler.start()
    assert handler._mqtt is replacement
    replacement.connect.assert_called_once()
    assert handler.stop()
    assert handler._mqtt is None
    replacement.stop.assert_called_once()


@pytest.mark.parametrize("storage_failure", [False, True])
def test_fatal_worker_errors_stop_without_retry_or_blocking_polling(integration, monkeypatch, storage_failure):
    async def scenario():
        handler, mqtt, _ = make_handler(integration)
        error = EzvizTokenPersistenceError() if storage_failure else EzvizPushFatalError()
        mqtt.raise_if_failed.side_effect = error
        issue = MagicMock()
        monkeypatch.setattr(integration.mqtt.ir, "async_create_issue", issue)
        handler.async_start()
        await asyncio.wait_for(handler._task, 1)
        mqtt.connect.assert_called_once()
        mqtt.stop.assert_called_once()
        if storage_failure:
            issue.assert_called_once()
            handler._config_entry.async_start_reauth.assert_not_called()
        else:
            handler._config_entry.async_start_reauth.assert_called_once()
        assert await handler.async_stop()

    asyncio.run(scenario())
