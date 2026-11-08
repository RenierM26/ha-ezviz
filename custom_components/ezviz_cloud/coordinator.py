"""Provides the ezviz DataUpdateCoordinator."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _InventoryResult:
    snapshot: Any
    cameras: dict


class EzvizDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching EZVIZ data."""

    data: dict[str, Any]

    def __init__(self, hass: HomeAssistant, *, api: EzvizClient, api_timeout: int) -> None:
        """Initialize global EZVIZ data updater."""
        self.ezviz_client = api
        self._api_timeout = api_timeout
        self._poll_future: asyncio.Future | None = None
        self.inventory_snapshot: Any = None
        self.inventory_data: dict = {}
        self._closing = False
        self.worker_owner: Any = None
        self._close_task: asyncio.Task | None = None
        update_interval = timedelta(seconds=30)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self) -> dict:
        """Fetch data from EZVIZ."""
        try:
            if self._closing:
                raise UpdateFailed("EZVIZ coordinator is shutting down")
            if self._poll_future is None or self._poll_future.done():
                self._poll_future = asyncio.ensure_future(
                    self.hass.async_add_executor_job(self._poll_sync)
                )
                self._poll_future.add_done_callback(_retrieve_worker_error)
            async with asyncio.timeout(self._api_timeout):
                result = await asyncio.shield(self._poll_future)
            if self._closing:
                raise UpdateFailed("EZVIZ coordinator is shutting down")
            if isinstance(result, _InventoryResult):
                self.inventory_snapshot = result.snapshot
                self.inventory_data = result.snapshot.as_dict()
                return result.cameras
            return deepcopy(result)

        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error

        except (InvalidURL, HTTPError, PyEzvizError, OSError, TimeoutError) as error:
            raise UpdateFailed(f"EZVIZ polling failed ({type(error).__name__})") from error

    def _poll_sync(self) -> _InventoryResult | dict:
        """One SDK poll, never a separate refresh per HA platform/device family."""
        poll = getattr(self.ezviz_client, "poll_devices", None)
        if callable(poll):
            # Keep the released legacy SDK usable until the new API is released.
            import pyezvizapi as sdk  # noqa: PLC0415

            snapshot = poll(sdk.DevicePollOptions(timeout=self._api_timeout))
            return _InventoryResult(snapshot, snapshot.as_dict("camera"))
        return self.ezviz_client.load_cameras()

    def device_data(self, serial: str) -> dict:
        """Use the camera push projection, otherwise the detached inventory view."""
        return (self.data or {}).get(serial, self.inventory_data.get(serial, {}))

    def devices_of_kind(self, kind: str) -> dict:
        """Selectors are cached views, not additional SDK/network calls."""
        if self.inventory_snapshot is None:
            return {}
        return self.inventory_snapshot.select(kind)

    def device_is_current(self, serial: str) -> bool:
        """Missing or failed device rendering never inherits stale online state."""
        if self.inventory_snapshot is None:
            return True  # Legacy SDK has only whole-poll health.
        health = self.inventory_snapshot.health.get(serial)
        return health is not None and health.present and health.current

    async def async_execute(self, func: Any, *args: Any) -> Any:
        """Keep new inventory command workers owned across cancellation/unload."""
        if self._closing:
            raise RuntimeError("EZVIZ coordinator is shutting down")
        if self.worker_owner is not None:
            return await self.worker_owner.async_run(func, *args)
        return await self.hass.async_add_executor_job(func, *args)

    def begin_shutdown(self) -> None:
        """Reject new polling work before waiting for optional push cleanup."""
        self._closing = True

    async def async_close(self) -> None:
        """Keep HTTP session ownership until the last polling worker has finished."""
        self.begin_shutdown()
        if self._close_task is None:
            self._close_task = self.hass.async_create_background_task(
                self._async_finish_close(), "EZVIZ HTTP cleanup"
            )
        await asyncio.shield(self._close_task)

    async def _async_finish_close(self) -> None:
        if self._poll_future is not None:
            with suppress(Exception):
                await asyncio.shield(self._poll_future)
        await self.hass.async_add_executor_job(self.ezviz_client.close_session)

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
