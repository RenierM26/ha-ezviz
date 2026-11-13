"""Own push, polling and HTTP cleanup even when unload's foreground wait ends."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EzvizDataUpdateCoordinator
    from .mqtt import EzvizMqttHandler
    from .tokens import TokenPersistence

from .workers import create_critical_task, create_executor_task

_LOGGER = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT = 5
CLEANUP_RETRY_SECONDS = 30


class RuntimeCleanup:
    """A single retained task closes HTTP only after push and polling finish."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        push: EzvizMqttHandler | None = None,
        persistence: TokenPersistence | None = None,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.push = push
        self.persistence = persistence
        self.task: asyncio.Task | None = None
        self._workers: set[asyncio.Future] = set()
        self._stopping = False
        self.cleanup_complete = False
        self.cleanup_error_type: str | None = None
        self._push_stopped = False
        self._http_closed = False

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
        if self.task is None or (self.task.done() and not self.cleanup_complete):
            self.cleanup_error_type = None
            self.task = create_critical_task(self.hass, self._finish(), "EZVIZ runtime cleanup")
            self.task.add_done_callback(self._cleanup_done)
        try:
            await asyncio.wait_for(asyncio.shield(self.task), SHUTDOWN_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("EZVIZ runtime cleanup still owns pending workers")

    async def async_wait_stopped(self) -> None:
        """HA shutdown must await the owned cleanup, not just its foreground bound."""
        while not self.cleanup_complete:
            try:
                await self.async_stop()
                if self.task is not None:
                    await asyncio.shield(self.task)
            except Exception:
                # Retain this owner through removal/HA stop, and retry stages
                # which did not finish; successful stages are not repeated.
                await asyncio.sleep(CLEANUP_RETRY_SECONDS)

    def _cleanup_done(self, task: asyncio.Task) -> None:
        """Observe late failures even if the unload caller stopped waiting."""
        if task.cancelled():
            self.cleanup_error_type = "CancelledError"
        elif error := task.exception():
            self.cleanup_error_type = type(error).__name__
            _LOGGER.warning("EZVIZ cleanup incomplete (%s); ownership retained", self.cleanup_error_type)

    async def _finish(self) -> None:
        errors: list[Exception] = []
        try:
            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)
            if not self._push_stopped:
                if self.push is not None:
                    await self.push.async_wait_stopped()
                self._push_stopped = True
            # Never close shared HTTP until push confirms that it is stopped.
            if not self._http_closed:
                await self.coordinator.async_close()
                self._http_closed = True
        except Exception as err:
            errors.append(err)
        finally:
            # Drain currently owned writes even when an earlier cleanup stage fails.
            # This alone is not completion: an un-stopped producer can enqueue more.
            if self.persistence is not None:
                try:
                    await self.persistence.async_drain()
                except Exception as err:
                    errors.append(err)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("EZVIZ cleanup stages failed", errors)
        self.cleanup_complete = True


type EzvizConfigEntry = ConfigEntry[RuntimeCleanup]
