"""Bounded pending settings and account-wide delayed cloud confirmation."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

CONFIRMATION_DELAY = 2.0
CONFIRMATION_RETRY = 10.0
PENDING_TIMEOUT = 60.0


@dataclass(frozen=True)
class PendingSetting:
    """One accepted command, separate from observed SDK/coordinator data."""

    generation: int
    value: Any
    read_observed: Callable[[], Any]
    expires: float


class SettingConfirmation:
    """Share setting write ownership, refreshes and expiry within one account."""

    def __init__(self, coordinator: EzvizDataUpdateCoordinator) -> None:
        """Initialize entry-scoped state; no timers exist before a write."""
        self.coordinator = coordinator
        self.generation = 0
        self.pending: dict[tuple[str, str], PendingSetting] = {}
        self._write_lock = asyncio.Lock()
        self._confirm_cancel: Callable[[], None] | None = None
        self._expiry_cancel: Callable[[], None] | None = None
        self._refresh_task: asyncio.Task | None = None
        self._confirmation_due = 0.0

    def value(self, serial: str, key: str, observed: Any) -> Any:
        """Read immediately; never wait or mutate an observation in a getter."""
        if pending := self.pending.get((serial, key)):
            if self.coordinator.hass.loop.time() < pending.expires:
                return pending.value
        return observed

    async def async_write(
        self,
        serial: str,
        key: str,
        value: Any,
        read_observed: Callable[[], Any],
        func: Callable,
        *args: Any,
    ) -> None:
        """Own acceptance across service cancellation and serialize cloud writes."""

        async def operation() -> None:
            async with self._write_lock:
                result = await self.coordinator.async_execute(func, *args)
                if result is False:
                    raise HomeAssistantError("EZVIZ did not accept the setting command")
                self.generation += 1
                self.pending[serial, key] = PendingSetting(
                    self.generation,
                    value,
                    read_observed,
                    self.coordinator.hass.loop.time() + PENDING_TIMEOUT,
                )
                self._schedule_confirmation(CONFIRMATION_DELAY)
                self._schedule_expiry()
                self.coordinator.async_update_listeners()

        await self.coordinator.async_execute_owned(operation)

    @callback
    def accept_poll(self, generation: int) -> None:
        """Confirm only current devices from a poll started after acceptance."""
        for identity, pending in tuple(self.pending.items()):
            serial, _key = identity
            state = self.coordinator.device_state(serial)
            eligible = (
                pending.generation <= generation
                and self.coordinator.device_is_current(serial)
                and state is not None
                and state.online is True
            )
            if not eligible:
                continue
            try:
                matches = pending.read_observed() == pending.value
            except KeyError, TypeError, ValueError:
                # A missing/unrenderable property cannot confirm a command or
                # turn an otherwise accepted account poll into a failure.
                continue
            if matches:
                del self.pending[identity]
        self._schedule_expiry()
        if not self.pending and self._confirm_cancel:
            self._confirm_cancel()
            self._confirm_cancel = None

    @callback
    def _schedule_confirmation(self, delay: float) -> None:
        if self._confirm_cancel:
            self._confirm_cancel()
        self._confirmation_due = self.coordinator.hass.loop.time() + delay
        self._confirm_cancel = async_call_later(
            self.coordinator.hass, delay, self._start_confirmation
        )

    @callback
    def _start_confirmation(self, _now: Any) -> None:
        self._confirm_cancel = None
        if self.coordinator.shutting_down or not self.pending or self._refresh_task:
            return
        self._refresh_task = self.coordinator.hass.async_create_background_task(
            self._async_confirm(), "EZVIZ setting confirmation", eager_start=False
        )

    async def _async_confirm(self) -> None:
        try:
            # Do not poll between related writes when a cloud call is slow.
            async with self._write_lock:
                pass
            if (
                not self.coordinator.shutting_down
                and self.pending
                and self.coordinator.hass.loop.time() >= self._confirmation_due
            ):
                await self.coordinator.async_request_refresh()
        finally:
            self._refresh_task = None
            if (
                not self.coordinator.shutting_down
                and self.pending
                and self._confirm_cancel is None
                and not isinstance(
                    self.coordinator.last_exception, ConfigEntryAuthFailed
                )
            ):
                self._schedule_confirmation(CONFIRMATION_RETRY)

    @callback
    def _schedule_expiry(self) -> None:
        if self._expiry_cancel:
            self._expiry_cancel()
            self._expiry_cancel = None
        if self.pending:
            delay = max(
                0,
                min(item.expires for item in self.pending.values())
                - self.coordinator.hass.loop.time(),
            )
            self._expiry_cancel = async_call_later(
                self.coordinator.hass, delay, self._expire
            )

    @callback
    def _expire(self, _now: Any) -> None:
        self._expiry_cancel = None
        now = self.coordinator.hass.loop.time()
        self.pending = {
            identity: item
            for identity, item in self.pending.items()
            if item.expires > now
        }
        self._schedule_expiry()
        if not self.pending and self._confirm_cancel:
            self._confirm_cancel()
            self._confirm_cancel = None
        self.coordinator.async_update_listeners()

    @callback
    def close(self) -> asyncio.Task | None:
        """Cancel callbacks and confirmation work before entry session cleanup."""
        for cancel in (self._confirm_cancel, self._expiry_cancel):
            if cancel:
                cancel()
        self._confirm_cancel = self._expiry_cancel = None
        self.pending.clear()
        task = self._refresh_task
        if task:
            task.cancel()
        return task
