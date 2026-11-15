"""Provides the ezviz DataUpdateCoordinator."""

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from copy import deepcopy
from datetime import timedelta
import logging
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any, override

from pyezvizapi import DevicePollOptions, DeviceSnapshot
from pyezvizapi.client import EzvizClient
from pyezvizapi.devices.snapshot import DeviceKind, DeviceState
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
from .errors import request_error
from .local_rtsp import (
    POLL_INTERVAL,
    LocalRtspConfig,
    read_local_rtsp,
    write_local_rtsp,
)
from .setting_writes import SettingWrites
from .workers import create_critical_task, create_executor_task

if TYPE_CHECKING:
    from .runtime import EzvizConfigEntry
_LOGGER = logging.getLogger(__name__)


class EzvizDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Class to manage fetching EZVIZ data."""

    data: dict[str, dict[str, Any]]

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        config_entry: EzvizConfigEntry | None,
        api: EzvizClient,
        api_timeout: int,
    ) -> None:
        """Initialize global EZVIZ data updater."""
        self.ezviz_client = api
        self._api_timeout = api_timeout
        self.credentials = CameraCredentials(hass, config_entry, self)
        self.settings = SettingWrites(self)
        self._update_lock = asyncio.Lock()
        self._poll_future: asyncio.Future[DeviceSnapshot] | None = None
        self.inventory_snapshot: DeviceSnapshot | None = None
        self.inventory_data: dict[str, dict[str, Any]] = {}
        self._closing = False
        self._image_downloads: dict[tuple[Any, ...], asyncio.Task] = {}
        self.image_records: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._action_futures: set[asyncio.Future] = set()
        self._close_task: asyncio.Task | None = None
        self._close_succeeded = False
        update_interval = timedelta(seconds=30)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval,
        )

    @override
    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch data from EZVIZ."""
        async with self._update_lock:
            return await self._async_fetch_inventory()

    async def _async_fetch_inventory(self) -> dict[str, dict[str, Any]]:
        """Serialize complete poll acceptance, including timed-out worker reuse."""
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
            inventory, cameras = await self.async_execute(_project_snapshot, result)
            self.inventory_snapshot = result
            self.inventory_data = inventory

        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error

        except (InvalidURL, HTTPError, PyEzvizError, OSError, TimeoutError) as error:
            raise UpdateFailed(str(request_error("Poll devices", error))) from error
        else:
            return cameras

    @property
    def poll_in_flight(self) -> bool:
        """Return whether an owned inventory worker is still running."""
        return self._poll_future is not None and not self._poll_future.done()

    def _poll_sync(self) -> DeviceSnapshot:
        """Require the new SDK's single deadline-bound mixed-family poll."""
        return self.ezviz_client.poll_devices(
            DevicePollOptions(timeout=self._api_timeout)
        )

    def register_image(self, serial: str, url: str) -> str:
        """Remember only SDK-provided images in a bounded account-local registry."""
        if serial not in (self.data or {}):
            raise ValueError("Unknown camera")
        record = (serial, url)
        for reference, existing in self.image_records.items():
            if existing == record:
                return reference
        reference = token_urlsafe(24)
        self.image_records[reference] = record
        while len(self.image_records) > 1024:
            self.image_records.popitem(last=False)
        return reference

    def device_data(self, serial: str) -> dict[str, Any]:
        """Use the camera push projection, otherwise the detached inventory view."""
        return (self.data or {}).get(serial, self.inventory_data.get(serial, {}))

    def devices_of_kind(self, kind: DeviceKind) -> dict[str, DeviceState]:
        """Selectors are cached views, not additional SDK/network calls."""
        if self.inventory_snapshot is None:
            return {}
        return self.inventory_snapshot.select(kind)

    def device_state(self, serial: str) -> DeviceState | None:
        """Return accepted SDK state, never the partial camera push overlay."""
        if self.inventory_snapshot is None:
            return None
        return self.inventory_snapshot.devices.get(serial)

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

    async def async_execute_owned(
        self, operation: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:
        """Own a complete side-effect/commit operation, not only its SDK worker."""
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        task = create_critical_task(
            self.hass, operation(), "EZVIZ credential operation"
        )
        self._action_futures.add(task)
        task.add_done_callback(self._action_futures.discard)
        task.add_done_callback(_retrieve_worker_error)
        return await asyncio.shield(task)

    async def async_image_download(
        self, key: tuple[Any, ...], operation: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:
        """Share only concurrent downloads for one account/camera/image generation."""
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        if key not in self._image_downloads:
            task = create_critical_task(self.hass, operation(), "EZVIZ image download")
            self._image_downloads[key] = task
            self._action_futures.add(task)

            def finished(future: asyncio.Task) -> None:
                self._image_downloads.pop(key, None)
                self._action_futures.discard(future)
                _retrieve_worker_error(future)

            task.add_done_callback(finished)
        return await asyncio.shield(self._image_downloads[key])

    @property
    def shutting_down(self) -> bool:
        """Expose the owned-worker shutdown boundary to image/action adapters."""
        return self._closing

    def begin_shutdown(self) -> None:
        """Reject new polling work before waiting for optional push cleanup."""
        self._closing = True
        if task := self.settings.close():
            self._action_futures.add(task)

    async def async_close(self) -> None:
        """Keep HTTP session ownership until the last polling worker has finished."""
        self.begin_shutdown()
        if self._close_task is None or (
            self._close_task.done() and not self._close_succeeded
        ):
            self._close_task = create_critical_task(
                self.hass, self._async_finish_close(), "EZVIZ HTTP cleanup"
            )
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
        if self._closing or not self.data or serial not in self.data:
            return
        ext = mqtt_data.get("ext")
        if not isinstance(ext, dict) or not ext.get("image"):
            return
        changes = {
            "last_alarm_type_code": ext.get("alert_type_code"),
            "last_alarm_time": ext.get("time"),
            "last_alarm_pic": ext.get("image"),
            "last_alarm_type_name": mqtt_data.get("alert"),
            "Motion_Trigger": True,
        }
        if all(self.data[serial].get(key) == value for key, value in changes.items()):
            return
        snapshot = dict(self.data)
        snapshot[serial] = deepcopy(self.data[serial])
        snapshot[serial].update(deepcopy(changes))

        # Push is a partial observation, not a successful inventory poll. Keep
        # last_update_success/last_exception untouched while notifying listeners.
        self.data = snapshot
        self.async_update_listeners()


def _project_snapshot(
    result: DeviceSnapshot,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Thaw once off-loop, keeping SDK command adapters detached from inventory."""
    inventory = result.as_dict()
    cameras = {
        serial: deepcopy(inventory[serial]) for serial in result.select("camera")
    }
    return inventory, cameras


def _retrieve_worker_error(future: asyncio.Future) -> None:
    """Retrieve late failures without cancelling a still-owned executor job."""
    if not future.cancelled():
        future.exception()


class LocalRtspCoordinator(DataUpdateCoordinator[bool]):
    """Poll one opted-in camera independently of cloud health and refreshes."""

    def __init__(
        self, account: EzvizDataUpdateCoordinator, serial: str, config: LocalRtspConfig
    ) -> None:
        """Initialize an account-owned local IO lifetime and per-camera lock."""
        super().__init__(
            account.hass,
            _LOGGER,
            config_entry=account.config_entry,
            name="EZVIZ local RTSP",
            update_interval=POLL_INTERVAL,
        )
        self.account = account
        self.serial = serial
        self.config = config
        self._io_lock = asyncio.Lock()

    @override
    async def _async_update_data(self) -> bool:
        try:
            async with self._io_lock:
                return await self.account.async_execute(
                    read_local_rtsp, self.config, self.serial
                )
        except PyEzvizError, OSError, ValueError:
            # Never include device responses, addresses or credential-bearing errors.
            raise UpdateFailed("Local RTSP configuration unavailable") from None

    async def async_set_enabled(self, enabled: bool) -> None:
        """Own the complete write/read-back even if the service caller cancels."""

        async def operation() -> None:
            async with self._io_lock:
                try:
                    observed = await self.account.async_execute(
                        write_local_rtsp, self.config, self.serial, enabled
                    )
                except PyEzvizError, OSError, ValueError:
                    error = UpdateFailed("Local RTSP change refused or not verified")
                    self.async_set_update_error(error)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="local_rtsp_unverified",
                    ) from None
                self.async_set_updated_data(observed)

        await self.account.async_execute_owned(operation)
