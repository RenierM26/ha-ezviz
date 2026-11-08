"""Durable token acknowledgements, reauth invalidation and SDK feature gating."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/ezviz_cloud"


@pytest.fixture
def tokens(monkeypatch):
    package = ModuleType("token_test")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "token_test", package)
    monkeypatch.setitem(sys.modules, "token_test.const", SimpleNamespace(CONF_SDK_TOKEN="sdk_token", DOMAIN="ezviz_cloud"))
    class Store:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"private": True, "atomic_writes": True}
            self.saved = None
            self.failure = None
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.release.set()
        async def async_load(self):
            return deepcopy(self.saved)
        async def async_save(self, value):
            self.entered.set()
            await self.release.wait()
            if self.failure:
                raise self.failure
            self.saved = deepcopy(value)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", SimpleNamespace(Store=Store))
    spec = importlib.util.spec_from_file_location("token_test.tokens", ROOT / "tokens.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "token_test.tokens", module)
    spec.loader.exec_module(module)
    return module


def persistence(tokens, session="one", initial=None):
    entry = SimpleNamespace(entry_id="entry", data={"session_id": session, "user_id": "same-user", "url": "api.test", "sdk_token": initial})
    return tokens.TokenPersistence(SimpleNamespace(loop=asyncio.get_running_loop()), entry)


def test_worker_ack_waits_for_completed_atomic_save_and_copies_input(tokens):
    async def scenario():
        value = persistence(tokens)
        value._store.release.clear()
        token = {"session_id": "SYNTHETIC", "push_state": {"counter": 1}}
        task = asyncio.create_task(asyncio.to_thread(value.save_from_worker, token))
        await value._store.entered.wait()
        token["push_state"]["counter"] = 2
        assert not task.done()
        value._store.release.set()
        await task
        assert (await value.async_load())["push_state"]["counter"] == 1
    asyncio.run(scenario())


def test_failed_storage_never_acknowledges_and_loop_call_is_rejected(tokens):
    async def scenario():
        value = persistence(tokens)
        value._store.failure = OSError("synthetic storage failure")
        with pytest.raises(OSError):
            await asyncio.to_thread(value.save_from_worker, {"session_id": "new"})
        assert value._store.saved is None
        with pytest.raises(RuntimeError, match="executor"):
            value.save_from_worker({})
    asyncio.run(scenario())


def test_reauth_anchor_rejects_old_store_and_uses_new_full_entry_token(tokens):
    async def scenario():
        old = persistence(tokens)
        await old.async_save({"session_id": "rotated-old"})
        new = persistence(tokens, session="new-login", initial={"session_id": "fresh", "push_profile": "synthetic"})
        new._store.saved = old._store.saved
        assert (await new.async_load())["session_id"] == "fresh"
        assert (await old.async_load())["session_id"] == "rotated-old"
    asyncio.run(scenario())


def test_fresh_login_uses_explicit_migration_but_old_sdk_keeps_polling(tokens):
    calls = []
    class Old:
        def login(self, *args):
            calls.append(("old", args))
            return {}
    class New(Old):
        def __init__(self, *, on_token_updated=None):
            pass
        def enable_channel99(self, *args):
            calls.append(("new", args))
            return {}
    assert not tokens.supports_durable_callback(Old)
    assert tokens.supports_durable_callback(New)
    tokens.credential_login(Old())
    tokens.credential_login(New(), "123456")
    assert calls == [("old", ()), ("new", ("123456",))]
