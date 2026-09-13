"""Offline regressions for optional push using a minimal HA boundary."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.exceptions import HTTPError
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
        "push_test.const", "push_test.coordinator", "push_test.views",
    ):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    constants = sys.modules["push_test.const"]
    for name in (
        "DOMAIN", "DATA_COORDINATOR", "MQTT_HANDLER", "ATTR_TYPE_CLOUD",
        "CONF_SESSION_ID", "CONF_RF_SESSION_ID", "CONF_USER_ID",
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
    return integration.mqtt.EzvizMqttHandler(hass, client, "entry"), mqtt, hass


@pytest.mark.parametrize("error", [HTTPError(), OSError(), TimeoutError(), ValueError()])
def test_failure_and_cleanup_error_do_not_escape_and_retry_recovers(integration, monkeypatch, error):
    async def scenario():
        handler, mqtt, _ = make_handler(integration)
        mqtt.connect.side_effect = [error, None]
        mqtt.stop.side_effect = HTTPError()
        monkeypatch.setattr(integration.mqtt, "PUSH_RETRY_SECONDS", 0.001)
        handler.async_start()
        await asyncio.wait_for(handler._task, 1)
        assert mqtt.connect.call_count == 2
        assert mqtt.stop.call_count == 1
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
        await handler._task
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
        entry.data = {**token, "conf_type": "attr_type_cloud", "conf_url": "api.test", "conf_user_id": "user"}
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


def test_unload_platforms_even_when_push_stop_fails(integration):
    async def scenario():
        handler, mqtt, hass = make_handler(integration)
        hass.data["domain"]["entry"]["mqtt_handler"] = handler
        hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        entry = SimpleNamespace(entry_id="entry")
        handler.async_start()
        await handler._task
        mqtt.stop.side_effect = HTTPError()
        assert await integration.setup.async_unload_entry(hass, entry)
        hass.config_entries.async_unload_platforms.assert_awaited_once()
        assert "entry" not in hass.data["domain"]

    asyncio.run(scenario())
