"""Durable channel-99 snapshots using HA's public, private atomic Store API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
import hashlib
from inspect import signature
import json
from typing import Any

from homeassistant.helpers.storage import Store

from .const import CONF_SDK_TOKEN, DOMAIN

PERSIST_TIMEOUT = 30


class AcknowledgedStore(Store):
    """HA's ordinary save intentionally swallows failures/defers at shutdown.

    Narrow, real-HA-tested hook: distinguish completed fsynced writes from a
    normal return without a write, and propagate errors before HA logs them.
    No direct .storage editing or global Store monkeypatching is involved.
    """

    async def async_save(self, data: Any) -> None:
        self._write_acknowledged = False
        await super().async_save(data)
        if not self._write_acknowledged:
            raise OSError("EZVIZ token persistence did not complete a durable write")

    async def _async_write_data(self, data: dict) -> None:
        try:
            await super()._async_write_data(data)
        except Exception as error:
            # Store catches WriteError/SerializationError and would otherwise ACK.
            raise OSError("EZVIZ durable token write failed") from error
        self._write_acknowledged = True


def supports_durable_callback(client_type: Any) -> bool:
    """Released older SDKs may not yet provide the new persistence contract."""
    return "on_token_updated" in signature(client_type).parameters


def credential_login(client: Any, code: str | None = None) -> dict:
    """A real credential login can migrate profile; stored passwords are unnecessary."""
    login = getattr(client, "enable_channel99", client.login)
    try:
        return deepcopy(login() if code is None else login(code))
    finally:
        close = getattr(client, "close_session", None)
        if callable(close):
            close()


class TokenPersistence:
    """Serialize saves; worker acknowledgement waits for the fsynced write."""

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry
        anchor = {key: entry.data.get(key) for key in ("session_id", "user_id", "url")}
        self._anchor = hashlib.sha256(json.dumps(anchor, sort_keys=True).encode()).hexdigest()
        self._store = AcknowledgedStore(
            hass, 1, f"{DOMAIN}.token.{entry.entry_id}", private=True, atomic_writes=True
        )
        self._lock = asyncio.Lock()

    async def async_load(self) -> dict | None:
        """Ignore a pre-reauth store, never roll a new credential login backwards."""
        saved = await self._store.async_load()
        if isinstance(saved, Mapping) and saved.get("anchor") == self._anchor:
            token = saved.get("token")
            if isinstance(token, Mapping):
                return deepcopy(dict(token))
        token = self._entry.data.get(CONF_SDK_TOKEN)
        return deepcopy(dict(token)) if isinstance(token, Mapping) else None

    async def async_save(self, token: dict) -> None:
        """Completion means the private atomic write (including fsync) finished."""
        async with self._lock:
            await self._store.async_save({"anchor": self._anchor, "token": deepcopy(token)})

    async def async_remove(self) -> None:
        """Remove credentials through the public HA storage lifecycle."""
        async with self._lock:
            await self._store.async_remove()

    def save_from_worker(self, token: dict) -> None:
        """Never acknowledge a queued/delayed HA config-entry update as durable."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self._hass.loop:
            raise RuntimeError("EZVIZ persistence must run in an executor, not the HA loop")
        future = asyncio.run_coroutine_threadsafe(self.async_save(deepcopy(token)), self._hass.loop)
        # A timeout is a persistence failure; it must propagate before SDK ACK.
        # Do not cancel a write whose OS operation may already be running.
        future.result(timeout=PERSIST_TIMEOUT)
