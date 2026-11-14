"""Durable channel-99 snapshots using HA's public, private atomic Store API."""

import asyncio
from collections.abc import Mapping
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from threading import RLock
from typing import Any, TypeGuard, override
from uuid import uuid4

from homeassistant.const import CONF_TYPE, CONF_URL, CONF_USERNAME
from homeassistant.helpers.storage import Store

from .const import ATTR_TYPE_CLOUD, CONF_AUTH_REF, CONF_SDK_TOKEN, DOMAIN
from .workers import create_critical_task

PERSIST_TIMEOUT = 30


class AcknowledgedStore(Store):
    """HA's ordinary save intentionally swallows failures/defers at shutdown.

    Narrow, real-HA-tested hook: distinguish completed fsynced writes from a
    normal return without a write, and propagate errors before HA logs them.
    No direct .storage editing or global Store monkeypatching is involved.
    """

    _write_acknowledged: bool = False

    @override
    async def async_save(self, data: Any) -> None:
        """Persist a snapshot and require an acknowledged atomic write."""
        self._write_acknowledged = False
        await super().async_save(data)
        if not self._write_acknowledged:
            raise OSError("EZVIZ token persistence did not complete a durable write")

    @override
    async def _async_write_data(self, data: dict) -> None:
        try:
            await super()._async_write_data(data)
        except Exception as error:
            # Store catches WriteError/SerializationError and would otherwise ACK.
            raise OSError("EZVIZ durable token write failed") from error
        self._write_acknowledged = True


@dataclass
class _IdentityState:
    store: AcknowledgedStore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    needs_ack: bool = False


def _identity_state(hass: Any, account: str) -> _IdentityState:
    # Initial flows have no entry/runtime_data; retain their shared lock through
    # setup, reauth and unload so competing first logins cannot select two IDs.
    registry = hass.data.setdefault(DOMAIN, {}).setdefault("_identity_stores", {})  # pylint: disable=home-assistant-use-runtime-data
    if account not in registry:
        registry[account] = _IdentityState(
            AcknowledgedStore(
                hass,
                1,
                f"{DOMAIN}.identity.{account}",
                private=True,
                atomic_writes=True,
            )
        )
    return registry[account]


def _valid_identity(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


async def _async_identity(hass: Any, account: str, token: Mapping | None) -> str:
    async def resolve() -> str:
        state = _identity_state(hass, account)
        async with state.lock:
            record = await state.store.async_load()
            if record is not None:
                if not isinstance(record, Mapping) or not _valid_identity(
                    record.get("feature_code")
                ):
                    raise OSError("Invalid saved EZVIZ installation identity")
                if not state.needs_ack:
                    return record["feature_code"]
                identity = record["feature_code"]
            else:
                identity = None
            # Explicitly adopt a trusted existing token's identity, even when the
            # container MAC changed. Never rewrite its credentials/replay state.
            if identity is None:
                candidate = (token or {}).get("feature_code")
                identity = candidate if _valid_identity(candidate) else uuid4().hex
            # Store can expose its deferred in-memory data after a shutdown save
            # failed to ACK. Do not trust that data without a successful write.
            state.needs_ack = True
            await state.store.async_save({"feature_code": identity})
            state.needs_ack = False
            return identity

    task = create_critical_task(hass, resolve(), "EZVIZ installation identity save")
    return await asyncio.shield(task)


async def async_login_identity(hass: Any, account: str) -> str:
    """Durably choose an account's installation identity before its first login."""
    return await _async_identity(
        hass, hashlib.sha256(account.encode()).hexdigest(), None
    )


def credential_login(client: Any, code: str | None = None) -> dict:
    """A real credential login can migrate profile; stored passwords are unnecessary."""
    try:
        return deepcopy(
            client.enable_channel99() if code is None else client.enable_channel99(code)
        )
    finally:
        client.close_session()


def _account(entry: Any) -> str:
    identity = getattr(entry, "unique_id", None) or entry.entry_id
    return hashlib.sha256(str(identity).encode()).hexdigest()


def compact_entry_data(
    entry: Any, api_url: str | None = None, login_name: str | None = None
) -> dict[str, Any]:
    """Public routing metadata only; retain a legacy login name only when necessary."""
    data = {
        CONF_TYPE: ATTR_TYPE_CLOUD,
        CONF_URL: api_url or entry.data.get(CONF_URL, "apiieu.ezvizlife.com"),
    }
    if not getattr(entry, "unique_id", None) and (
        login_name or entry.data.get(CONF_USERNAME)
    ):
        data[CONF_USERNAME] = login_name or entry.data[CONF_USERNAME]
    return data


@dataclass
class _TokenState:
    store: AcknowledgedStore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    record: dict | None = field(default=None, repr=False)
    loaded: bool = False
    removed: bool = False
    revision: str | None = None


def _pending_store(hass: Any, reference: str) -> AcknowledgedStore:
    return AcknowledgedStore(
        hass, 1, f"{DOMAIN}.pending_auth.{reference}", private=True, atomic_writes=True
    )


async def stage_login(hass: Any, account: str, token: dict) -> str:
    """Fsync a new login before HA has allocated its config-entry ID."""
    reference = uuid4().hex
    store = _pending_store(hass, reference)
    record = {
        "account": hashlib.sha256(account.encode()).hexdigest(),
        "revision": reference,
        "token": deepcopy(token),
    }
    task = create_critical_task(
        hass, store.async_save(record), "EZVIZ staged login save"
    )
    try:
        await asyncio.shield(task)
    except BaseException:

        async def discard() -> None:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            if not any(
                e.data.get(CONF_AUTH_REF) == reference
                for e in hass.config_entries.async_entries(DOMAIN)
            ):
                await store.async_remove()

        create_critical_task(hass, discard(), "EZVIZ abandoned login cleanup")
        raise
    return reference


class TokenPersistence:
    """One authoritative SDK snapshot, with shared locking and stale-owner rejection."""

    def __init__(self, hass: Any, entry: Any) -> None:
        """Initialize the owner and its cached dependencies."""
        self._hass = hass
        self._entry = entry
        anchor = {
            key: entry.data.get(key)
            for key in ("session_id", "rf_session_id", "user_id", "url", CONF_SDK_TOKEN)
        }
        self._anchor = hashlib.sha256(
            json.dumps(anchor, sort_keys=True).encode()
        ).hexdigest()
        self._account = _account(entry)
        # Share locks/revisions beyond runtime_data clearing during unload.
        registry = hass.data.setdefault(DOMAIN, {}).setdefault("_token_stores", {})  # pylint: disable=home-assistant-use-runtime-data
        if entry.entry_id not in registry:
            registry[entry.entry_id] = _TokenState(
                AcknowledgedStore(
                    hass,
                    1,
                    f"{DOMAIN}.token.{entry.entry_id}",
                    private=True,
                    atomic_writes=True,
                )
            )
        self._state = registry[entry.entry_id]
        self._store = self._state.store
        self._lock = self._state.lock
        self._revision: str | None = None
        self._bound = False
        self._pending_lock = RLock()
        self._pending: set[Future] = set()
        self._removed = False

    async def _ensure_loaded(self) -> None:
        if not self._state.loaded:
            loaded = await self._store.async_load()
            if loaded is not None and not isinstance(loaded, Mapping):
                raise OSError("Invalid EZVIZ credential record")
            self._state.record = dict(loaded) if loaded is not None else None
            self._state.loaded = True
            self._state.revision = (self._state.record or {}).get("revision")

    def _token(self) -> dict | None:
        saved = self._state.record
        if self._state.removed:
            return None
        if isinstance(saved, Mapping):
            if "revision" in saved:
                if saved.get("account") != self._account:
                    return None
                token = saved.get("token")
                return deepcopy(dict(token)) if isinstance(token, Mapping) else None
            if saved.get("anchor") == self._anchor and isinstance(
                saved.get("token"), Mapping
            ):
                return deepcopy(dict(saved["token"]))
        token = self._entry.data.get(CONF_SDK_TOKEN)
        if isinstance(token, Mapping):
            return deepcopy(dict(token))
        data = self._entry.data
        legacy = {
            key: data[key] for key in ("session_id", "rf_session_id") if key in data
        }
        if legacy:
            legacy["api_url"] = data.get(CONF_URL, "apiieu.ezvizlife.com")
            if data.get("user_id"):
                legacy["username"] = data["user_id"]
            return legacy
        return None

    async def async_identity(self, token: dict | None = None) -> str:
        """Select the persisted identity, adopting trusted old credentials once."""
        if token is None:
            token = await self.async_load()
        return await _async_identity(self._hass, self._account, token)

    async def async_load(self) -> dict | None:
        """Load or promote the private account snapshot."""
        if self._entry.data.get(CONF_AUTH_REF):
            await self.async_promote()
        async with self._lock:
            await self._ensure_loaded()
            if self._bound and self._revision != self._state.revision:
                return None
            self._revision = self._state.revision
            self._bound = True
            return self._token()

    async def _write(self, token: dict, revision: str) -> None:
        record = {
            "account": self._account,
            "revision": revision,
            "token": deepcopy(token),
        }
        await self._store.async_save(record)
        self._state.record = record
        self._state.revision = revision
        self._state.loaded = True
        self._revision = revision
        self._bound = True

    async def async_promote(self, *, version: int | None = None) -> None:
        """Move legacy/staged credentials before stripping any public data."""

        async def promote() -> None:
            async with self._lock:
                await self._ensure_loaded()
                if self._state.removed:
                    raise OSError("EZVIZ token owner has been removed")
                reference = self._entry.data.get(CONF_AUTH_REF)
                record = self._state.record or {}
                if reference and record.get("revision") != reference:
                    staged = await _pending_store(self._hass, reference).async_load()
                    if (
                        not isinstance(staged, Mapping)
                        or staged.get("account") != self._account
                    ):
                        raise OSError("Staged EZVIZ credentials are unavailable")
                    token = staged.get("token")
                    if not isinstance(token, Mapping):
                        raise OSError("Staged EZVIZ credentials are invalid")
                    await self._write(dict(token), reference)
                elif "revision" not in record:
                    await self._write(self._token() or {}, uuid4().hex)
                elif record.get("account") != self._account or not isinstance(
                    record.get("token"), Mapping
                ):
                    raise OSError(
                        "Saved EZVIZ credentials are invalid for this account"
                    )
                else:
                    self._revision = self._state.revision
                if reference:
                    await _pending_store(self._hass, reference).async_remove()
                update: dict[str, Any] = {"data": compact_entry_data(self._entry)}
                if version is not None:
                    update["version"] = version
                self._hass.config_entries.async_update_entry(self._entry, **update)

        task = create_critical_task(self._hass, promote(), "EZVIZ credential migration")
        await asyncio.shield(task)

    async def async_replace(
        self, token: dict, *, api_url: str | None = None, login_name: str | None = None
    ) -> None:
        """Reauth atomically supersedes all old SDK writers without a public token copy."""

        async def replace() -> None:
            async with self._lock:
                if self._state.removed:
                    raise OSError("EZVIZ token owner has been removed")
                if (
                    self._hass.config_entries.async_get_entry(self._entry.entry_id)
                    is not self._entry
                ):
                    raise OSError("EZVIZ token owner has been removed")
                await self._write(token, uuid4().hex)
                if (
                    self._hass.config_entries.async_get_entry(self._entry.entry_id)
                    is not self._entry
                ):
                    raise OSError("EZVIZ token owner has been removed")
                if reference := self._entry.data.get(CONF_AUTH_REF):
                    await _pending_store(self._hass, reference).async_remove()
                self._hass.config_entries.async_update_entry(
                    self._entry,
                    data=compact_entry_data(self._entry, api_url, login_name),
                )

        task = create_critical_task(
            self._hass, replace(), "EZVIZ replacement login save"
        )
        await asyncio.shield(task)

    async def async_save(self, token: dict) -> None:
        """Persist a snapshot and require an acknowledged atomic write."""
        async with self._lock:
            await self._ensure_loaded()
            if self._removed or self._state.removed:
                raise OSError("EZVIZ token owner has been removed")
            record = self._state.record or {}
            if "revision" in record and record.get("account") != self._account:
                raise OSError("Saved EZVIZ credentials belong to another account")
            if not self._bound:
                self._revision = self._state.revision
                self._bound = True
            if self._revision != self._state.revision:
                raise OSError("EZVIZ credential writer has been superseded")
            await self._write(token, self._revision or uuid4().hex)

    async def async_remove(self) -> None:
        """Remove the private snapshot after retained writers complete."""
        with self._pending_lock:
            self._removed = True
        async with self._lock:
            self._state.removed = True
        await self.async_drain()
        async with self._lock:
            await self._store.async_remove()
            if reference := self._entry.data.get(CONF_AUTH_REF):
                await _pending_store(self._hass, reference).async_remove()
            self._state.record = None
            identity = _identity_state(self._hass, self._account)
            async with identity.lock:
                await identity.store.async_remove()
                identity.needs_ack = False

    async def async_drain(self) -> None:
        """Join pending credential writes without discarding ownership."""
        while True:
            with self._pending_lock:
                pending = tuple(self._pending)
            if not pending:
                return
            await asyncio.gather(
                *(asyncio.shield(asyncio.wrap_future(future)) for future in pending),
                return_exceptions=True,
            )

    def _save_finished(self, future: Future) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    async def _async_owned_save(self, token: dict) -> None:
        """Keep HA from cancelling a Store's executor future during stop phase 1."""
        task = create_critical_task(
            self._hass, self.async_save(token), "EZVIZ durable token save"
        )
        await asyncio.shield(task)

    def save_from_worker(self, token: dict) -> None:
        """Never acknowledge a queued/delayed HA config-entry update as durable."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self._hass.loop:
            raise RuntimeError(
                "EZVIZ persistence must run in an executor, not the HA loop"
            )
        with self._pending_lock:
            if self._removed:
                raise OSError("EZVIZ token owner has been removed")
            future = asyncio.run_coroutine_threadsafe(
                self._async_owned_save(deepcopy(token)), self._hass.loop
            )
            self._pending.add(future)
            future.add_done_callback(self._save_finished)
        # A timeout is a persistence failure; it must propagate before SDK ACK.
        # Do not cancel a write whose OS operation may already be running.
        future.result(timeout=PERSIST_TIMEOUT)
