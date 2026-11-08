"""Durable token acknowledgements, reauth invalidation and channel-99 credential login."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

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
            await self._async_write_data(value)
        async def _async_write_data(self, value):
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
        token: dict[str, Any] = {"session_id": "SYNTHETIC", "push_state": {"counter": 1}}
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


def test_fresh_login_requires_channel99_and_closes_the_credential_client(tokens):
    calls = []
    class Client:
        def login(self, *args):
            pytest.fail("Credential login must use channel99")
        def enable_channel99(self, *args):
            calls.append(("channel99", args))
            return {"session_id": "synthetic"}
        def close_session(self):
            calls.append(("close", ()))
    assert tokens.credential_login(Client(), "123456") == {"session_id": "synthetic"}
    assert calls == [("channel99", ("123456",)), ("close", ())]
