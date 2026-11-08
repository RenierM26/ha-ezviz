"""HA-facing state contracts without starting HA or contacting devices."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/ezviz_cloud"


def load(monkeypatch, name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def coordinator_module(monkeypatch):
    package = ModuleType("contract_test")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "contract_test", package)
    monkeypatch.setitem(sys.modules, "contract_test.const", SimpleNamespace(DOMAIN="ezviz_cloud"))
    class Base:
        def __init__(self, hass, *args, **kwargs):
            self.hass = hass
            self.data = {}
            self.last_update_success = True
        def async_set_updated_data(self, value):
            self.data = value
    class Failed(Exception):
        pass
    for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers"):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", SimpleNamespace(ConfigEntryAuthFailed=Failed))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.update_coordinator",
                        SimpleNamespace(DataUpdateCoordinator=Base, UpdateFailed=Failed))
    return load(monkeypatch, "contract_test.coordinator", "coordinator.py")


@pytest.mark.parametrize("status,expected", [(1, True), ("1", True), (2, False), ("2", False),
    (None, False), (True, False), (False, False), (99, False), ("bad", False), (0, False)])
def test_availability_requires_poll_success_and_known_online(monkeypatch, status, expected):
    module = load(monkeypatch, "availability_test", "availability.py")
    coordinator = SimpleNamespace(last_update_success=True, data={"ONE": {"status": status}})
    assert module.device_available(coordinator, "ONE") is expected
    coordinator.last_update_success = False
    assert not module.device_available(coordinator, "ONE")
    coordinator.last_update_success = True
    assert not module.device_available(coordinator, "MISSING")
    coordinator.data = None
    assert not module.device_available(coordinator, "ONE")


def test_push_does_not_mutate_previous_snapshot_or_create_phantom_device(coordinator_module):
    c = coordinator_module.EzvizDataUpdateCoordinator(None, api=None, api_timeout=1)
    before = {"ONE": {"status": 1, "Motion_Trigger": False, "nested": {"value": 1}}}
    retained = deepcopy(before)
    c.data = before
    c.merge_mqtt_update("ONE", {"ext": {"image": "synthetic", "alert_type_code": 1}})
    assert before == retained
    assert c.data is not before and c.data["ONE"] is not before["ONE"]
    assert c.data["ONE"]["Motion_Trigger"] is True
    c.merge_mqtt_update("UNKNOWN", {"ext": {"image": "unused"}})
    assert set(c.data) == {"ONE"}
    c.merge_mqtt_update("ONE", {"ext": None})


def test_timed_out_poll_keeps_one_worker_until_it_finishes(coordinator_module):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def executor(func):
            calls.append(func)
            entered.set()
            await release.wait()
            return {"ONE": {"status": 1}}
        c = coordinator_module.EzvizDataUpdateCoordinator(SimpleNamespace(async_add_executor_job=executor),
            api=SimpleNamespace(load_cameras=lambda: None), api_timeout=0.01)
        try:
            for _ in range(2):
                with pytest.raises(coordinator_module.UpdateFailed):
                    await c._async_update_data()
            assert len(calls) == 1 and not c._poll_future.done()
            waiting = asyncio.create_task(c._async_update_data())
            await asyncio.sleep(0)
            release.set()
            value = await waiting
            assert value == {"ONE": {"status": 1}} and len(calls) == 1
        finally:
            release.set()
            await c._poll_future
    asyncio.run(scenario())
