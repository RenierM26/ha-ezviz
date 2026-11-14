"""Critical work must survive HA's early background-task cancellation."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def create_critical_task(hass: HomeAssistant, coro: Coroutine, name: str) -> Any:
    """Track before execution and join during HA's integration-stop phase.

    Merely tracking a task is insufficient: HA snapshots pre-stop tasks and can
    cancel them at final-write shutdown. A temporary stop listener joins this
    owner during the integration grace period. Completion removes the listener.
    """
    task = hass.async_create_task(coro, name, eager_start=False)

    async def wait_for_worker(_event: Any) -> None:
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    remove_listener = hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, wait_for_worker)
    task.add_done_callback(lambda _task: remove_listener())
    return task


def create_executor_task(hass: HomeAssistant, func: Callable, *args: Any) -> Any:
    """Put the owner in HA's tracked set before it dispatches an executor job."""

    async def run() -> Any:
        return await hass.async_add_executor_job(func, *args)

    return create_critical_task(hass, run(), "EZVIZ executor worker")
