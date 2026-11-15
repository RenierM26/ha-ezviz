"""Serialize settings and request one delayed, observed-only account refresh."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

REFRESH_DELAY = 2.0
# Command construction only: never presented as device state or confirmation.
COMMAND_CONTEXT_TTL = 60.0


@dataclass(frozen=True)
class SettingCommand:
    """A prepared SDK call and accepted sibling fields for subsequent writes."""

    func: Callable
    args: tuple[Any, ...]
    projections: (
        Callable[[], tuple[tuple[str, Callable[[dict[str, Any]], None]], ...]] | None
    ) = None


class SettingWrites:
    """Own writes and a single refresh timer within one config entry."""

    def __init__(self, coordinator: EzvizDataUpdateCoordinator) -> None:
        """Initialize without creating background work."""
        self.coordinator = coordinator
        self._context: dict[
            tuple[str, str], tuple[float, Callable[[dict[str, Any]], None]]
        ] = {}
        self._write_lock = asyncio.Lock()
        self._refresh_cancel: Callable[[], None] | None = None
        self._refresh_task: asyncio.Task | None = None
        self._refresh_due = 0.0

    def command_data(self, serial: str, observed: dict[str, Any]) -> dict[str, Any]:
        """Apply recent accepted sibling fields to detached SDK command input."""
        now = self.coordinator.hass.loop.time()
        self._context = {
            key: item for key, item in self._context.items() if item[0] > now
        }
        data = deepcopy(observed)
        for (owner, _key), (_expires, project) in self._context.items():
            if owner == serial:
                project(data)
        return data

    async def async_write(
        self,
        serial: str,
        func: Callable | None,
        *args: Any,
        prepare: Callable[[], SettingCommand] | None = None,
    ) -> None:
        """Prepare under the lock and retain ownership across caller cancellation."""

        async def operation() -> None:
            async with self._write_lock:
                if prepare:
                    command = prepare()
                else:
                    assert func is not None
                    command = SettingCommand(func, args)
                result = await self.coordinator.async_execute(
                    command.func, *command.args
                )
                if result is False:
                    raise HomeAssistantError("EZVIZ did not accept the setting command")
                expires = self.coordinator.hass.loop.time() + COMMAND_CONTEXT_TTL
                for key, project in (
                    command.projections() if command.projections else ()
                ):
                    self._context[serial, key] = (expires, project)
                self._refresh_due = self.coordinator.hass.loop.time() + REFRESH_DELAY
                self._schedule_refresh()

        await self.coordinator.async_execute_owned(operation)

    @callback
    def _schedule_refresh(self) -> None:
        if self._refresh_cancel:
            self._refresh_cancel()
        self._refresh_cancel = async_call_later(
            self.coordinator.hass,
            max(0, self._refresh_due - self.coordinator.hass.loop.time()),
            self._start_refresh,
        )

    @callback
    def _start_refresh(self, _now: Any) -> None:
        self._refresh_cancel = None
        if self.coordinator.shutting_down or self._refresh_task:
            return
        self._refresh_task = self.coordinator.hass.async_create_background_task(
            self._async_refresh(), "EZVIZ setting refresh", eager_start=False
        )

    async def _async_refresh(self) -> None:
        try:
            # A slow related write must finish and receive its own full delay.
            async with self._write_lock:
                if self.coordinator.hass.loop.time() < self._refresh_due:
                    return
                self._refresh_due = 0.0
            await self.coordinator.async_request_refresh()
        finally:
            self._refresh_task = None
            if (
                not self.coordinator.shutting_down
                and self._refresh_due
                and self._refresh_cancel is None
            ):
                self._schedule_refresh()

    @callback
    def close(self) -> asyncio.Task | None:
        """Cancel scheduled refresh work before the coordinator closes HTTP."""
        if self._refresh_cancel:
            self._refresh_cancel()
            self._refresh_cancel = None
        self._refresh_due = 0.0
        self._context.clear()
        if task := self._refresh_task:
            task.cancel()
        return self._refresh_task
