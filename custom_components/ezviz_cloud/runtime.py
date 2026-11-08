"""Own push, polling and HTTP cleanup even when unload's foreground wait ends."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .workers import create_critical_task, create_executor_task

_LOGGER = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT = 5


class RuntimeCleanup:
    """A single retained task closes HTTP only after push and polling finish."""

    def __init__(
        self, hass: Any, coordinator: Any, push: Any = None, persistence: Any = None
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.push = push
        self.persistence = persistence
        self.task: asyncio.Task | None = None
        self._workers: set[asyncio.Future] = set()
        self._stopping = False

    async def async_run(self, func: Any, *args: Any) -> Any:
        """A cancelled setup waiter must not orphan a login/token-save worker."""
        if self._stopping:
            raise RuntimeError("EZVIZ runtime is shutting down")
        future = create_executor_task(self.hass, func, *args)
        self._workers.add(future)
        future.add_done_callback(self._worker_done)
        return await asyncio.shield(future)

    def _worker_done(self, future: asyncio.Future) -> None:
        self._workers.discard(future)
        if not future.cancelled():
            future.exception()

    async def async_stop(self) -> None:
        self._stopping = True
        self.coordinator.begin_shutdown()
        if self.task is None:
            self.task = create_critical_task(self.hass, self._finish(), "EZVIZ runtime cleanup")
        try:
            await asyncio.wait_for(asyncio.shield(self.task), SHUTDOWN_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("EZVIZ runtime cleanup still owns pending workers")

    async def async_wait_stopped(self) -> None:
        """HA shutdown must await the owned cleanup, not just its foreground bound."""
        await self.async_stop()
        if self.task is not None:
            await asyncio.shield(self.task)

    async def _finish(self) -> None:
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        if self.push is not None:
            await self.push.async_wait_stopped()
        await self.coordinator.async_close()
        if self.persistence is not None:
            await self.persistence.async_drain()
