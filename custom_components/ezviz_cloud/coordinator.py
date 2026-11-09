"""Provides the ezviz DataUpdateCoordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from copy import deepcopy
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from pyezvizapi import DevicePollOptions, DeviceSnapshot
from pyezvizapi.client import EzvizClient
from pyezvizapi.devices.snapshot import DeviceKind
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .credentials import CameraCredentials
from .workers import create_critical_task, create_executor_task

if TYPE_CHECKING:
    from .runtime import EzvizConfigEntry
_LOGGER = logging.getLogger(__name__)


class EzvizDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching EZVIZ data."""

    data: dict[str, Any]

    def __init__(self, hass: HomeAssistant, *, config_entry: EzvizConfigEntry | None, api: EzvizClient, api_timeout: int) -> None:
        """Initialize global EZVIZ data updater."""
        self.ezviz_client = api
        self._api_timeout = api_timeout
        self.credentials = CameraCredentials(hass, config_entry, self)
        self._poll_future: asyncio.Future | None = None
        self.inventory_snapshot: DeviceSnapshot | None = None
        self.inventory_data: dict = {}
        self._closing = False
        self._action_futures: set[asyncio.Future] = set()
        self._close_task: asyncio.Task | None = None
        self._close_succeeded = False
        update_interval = timedelta(seconds=30)

        super().__init__(hass, _LOGGER, config_entry=config_entry, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self) -> dict:
        """Fetch data from EZVIZ."""
        try:
            if self._closing:
                raise UpdateFailed("EZVIZ coordinator is shutting down")
            if self._poll_future is None or self._poll_future.done():
                self._poll_future = create_executor_task(self.hass, self._poll_sync)
                self._poll_future.add_done_callback(_retrieve_worker_error)
            async with asyncio.timeout(self._api_timeout):
                result = await asyncio.shield(self._poll_future)
            if self._closing:
                raise UpdateFailed("EZVIZ coordinator is shutting down")
            self.inventory_snapshot = result
            self.inventory_data = result.as_dict()
            return result.as_dict("camera")

        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error

        except (InvalidURL, HTTPError, PyEzvizError, OSError, TimeoutError) as error:
            raise UpdateFailed(f"EZVIZ polling failed ({type(error).__name__})") from error

    def _poll_sync(self) -> DeviceSnapshot:
        """Require the new SDK's single deadline-bound mixed-family poll."""
        return self.ezviz_client.poll_devices(DevicePollOptions(timeout=self._api_timeout))

    def device_data(self, serial: str) -> dict:
        """Use the camera push projection, otherwise the detached inventory view."""
        return (self.data or {}).get(serial, self.inventory_data.get(serial, {}))

    def devices_of_kind(self, kind: DeviceKind) -> dict:
        """Selectors are cached views, not additional SDK/network calls."""
        if self.inventory_snapshot is None:
            return {}
        return self.inventory_snapshot.select(kind)

    def device_is_current(self, serial: str) -> bool:
        """Missing or failed device rendering never inherits stale online state."""
        if self.inventory_snapshot is None:
            return False
        health = self.inventory_snapshot.health.get(serial)
        return health is not None and health.present and health.current

    async def async_execute(self, func: Any, *args: Any) -> Any:
        """Keep new inventory command workers owned across cancellation/unload."""
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        future = create_executor_task(self.hass, func, *args)
        self._action_futures.add(future)
        future.add_done_callback(self._action_futures.discard)
        future.add_done_callback(_retrieve_worker_error)
        result = await asyncio.shield(future)
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        return result

    async def async_execute_owned(self, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """Own a complete side-effect/commit operation, not only its SDK worker."""
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        task = create_critical_task(self.hass, operation(), "EZVIZ credential operation")
        self._action_futures.add(task)
        task.add_done_callback(self._action_futures.discard)
        task.add_done_callback(_retrieve_worker_error)
        return await asyncio.shield(task)

    @property
    def shutting_down(self) -> bool:
        """Expose the owned-worker shutdown boundary to image/action adapters."""
        return self._closing

    def begin_shutdown(self) -> None:
        """Reject new polling work before waiting for optional push cleanup."""
        self._closing = True

    async def async_close(self) -> None:
        """Keep HTTP session ownership until the last polling worker has finished."""
        self.begin_shutdown()
        if self._close_task is None or (self._close_task.done() and not self._close_succeeded):
            self._close_task = create_critical_task(self.hass, self._async_finish_close(), "EZVIZ HTTP cleanup")
        await asyncio.shield(self._close_task)

    async def _async_finish_close(self) -> None:
        pending = tuple(self._action_futures)
        if self._poll_future is not None:
            pending += (self._poll_future,)
        if pending:
            await asyncio.gather(
                *(asyncio.shield(future) for future in pending), return_exceptions=True
            )
        await self.hass.async_add_executor_job(self.ezviz_client.close_session)
        self._close_succeeded = True

    def merge_mqtt_update(self, serial: str, mqtt_data: dict) -> None:
        """Merge MQTT update data into the coordinator."""

        # Push cannot invent inventory entries or mutate an earlier snapshot.
        if not self.data or serial not in self.data:
            return
        snapshot = deepcopy(self.data)
        ext = mqtt_data.get("ext")
        if not isinstance(ext, dict):
            return
        if ext.get("image"):
            snapshot[serial].update(
                last_alarm_type_code=ext.get("alert_type_code"),
                last_alarm_time=ext.get("time"),
                last_alarm_pic=ext.get("image"),
                last_alarm_type_name=mqtt_data.get("alert"),
                Motion_Trigger=True,
            )

        # Push is a partial observation, not a successful inventory poll. Keep
        # last_update_success/last_exception untouched while notifying listeners.
        self.data = snapshot
        self.async_update_listeners()


def _retrieve_worker_error(future: asyncio.Future) -> None:
    """Retrieve late failures without cancelling a still-owned executor job."""
    if not future.cancelled():
        future.exception()
