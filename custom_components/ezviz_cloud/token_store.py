"""Durable whole-token persistence shared by polling and push workers."""

import asyncio
from copy import deepcopy
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.json import load_json

from .const import CONF_TOKEN, DOMAIN


class DurableTokenStore(Store[dict[str, Any]]):
    """Surface failures/deferred saves that ordinary HA Store only logs."""

    _written = False

    async def async_save(self, data: dict[str, Any]) -> None:
        """Require completion, not a shutdown-delayed write acknowledgement."""
        if self.hass.state is CoreState.stopping:
            raise OSError("Home Assistant is stopping; token save not acknowledged")
        self._written = False
        await super().async_save(data)
        if not self._written:
            raise OSError("EZVIZ token storage did not complete")

    async def _async_write_data(self, data: dict) -> None:
        """Mark successful completion of the actual storage write."""
        await super()._async_write_data(data)
        self._written = True


class EzvizTokenStore:
    """Acknowledge SDK saves only after Home Assistant has written the snapshot."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind persistence to one credential login, not an obsolete reauth worker."""
        self.hass = hass
        self.entry = entry
        self.seed = entry.data[CONF_TOKEN]["session_id"]
        self.store = DurableTokenStore(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.token", private=True, atomic_writes=True
        )
        data = hass.data.setdefault(DOMAIN, {})
        self.removed: set[str] = data.setdefault("removed_token_stores", set())
        locks = data.setdefault("token_store_locks", {})
        self.lock: asyncio.Lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    async def async_load(self) -> dict[str, Any]:
        """Prefer rotating state only if it belongs to the current login."""
        async with self.lock:
            existed = await self.hass.async_add_executor_job(os.path.exists, self.store.path)
            # HA Store normally renames corrupt JSON and returns an empty state.
            # Preflight this owned token file so corruption stays fail-closed on
            # subsequent setup retries too, rather than creating another device.
            if existed:
                try:
                    await self.hass.async_add_executor_job(load_json, self.store.path)
                except HomeAssistantError as error:
                    raise OSError("EZVIZ token storage could not be decoded") from error
            saved = await self.store.async_load()
            if saved is None:
                if existed:
                    raise OSError("EZVIZ token storage could not be read")
                return deepcopy(self.entry.data[CONF_TOKEN])
            if (not isinstance(saved, dict) or not isinstance(saved.get("seed"), str)
                    or not isinstance(saved.get("token"), dict)):
                raise OSError("EZVIZ token storage is malformed")
            if saved["seed"] == self.seed:
                return deepcopy(saved["token"])
            return deepcopy(self.entry.data[CONF_TOKEN])

    async def async_save(self, snapshot: dict[str, Any]) -> None:
        """Serialize saves across reloads, rejecting superseded login state."""
        async with self.lock:
            if self.entry.entry_id in self.removed:
                raise RuntimeError("EZVIZ entry was removed; refusing credential save")
            if self.entry.data[CONF_TOKEN]["session_id"] != self.seed:
                raise RuntimeError("EZVIZ login changed during token persistence")
            await self.store.async_save({"seed": self.seed, "token": snapshot})
            if self.entry.data[CONF_TOKEN]["session_id"] != self.seed:
                raise RuntimeError("EZVIZ login changed during token persistence")

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        """Delete credentials after in-flight saves, blocking late recreation."""
        data = hass.data.setdefault(DOMAIN, {})
        lock = data.setdefault("token_store_locks", {}).setdefault(entry_id, asyncio.Lock())
        async with lock:
            data.setdefault("removed_token_stores", set()).add(entry_id)
            await DurableTokenStore(
                hass, 1, f"{DOMAIN}.{entry_id}.token", private=True, atomic_writes=True
            ).async_remove()

    def save(self, snapshot: dict[str, Any]) -> None:
        """SDK worker callback; never call from the Home Assistant event loop."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.hass.loop:
            raise RuntimeError("EZVIZ token callback must run in an executor")
        asyncio.run_coroutine_threadsafe(
            self.async_save(deepcopy(snapshot)), self.hass.loop
        ).result()
