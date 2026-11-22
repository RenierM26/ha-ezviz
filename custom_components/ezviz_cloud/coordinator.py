"""Provides the ezviz DataUpdateCoordinator."""

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
import logging
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any, override

from pyezvizapi import DevicePollOptions, DeviceSnapshot
from pyezvizapi.client import EzvizClient
from pyezvizapi.devices.channels import (
    camera_channel_notification,
    camera_channels,
    resolve_camera_channel,
)
from pyezvizapi.devices.intercom_events import intercom_event_type
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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .credentials import CameraCredentials
from .errors import request_error
from .intercom_events import IntercomEvents, event_zones
from .local import LocalDevice
from .local_rtsp import read_local_rtsp, write_local_rtsp
from .setting_writes import SettingWrites
from .workers import create_critical_task, create_executor_task

if TYPE_CHECKING:
    from .runtime import EzvizConfigEntry
_LOGGER = logging.getLogger(__name__)


# URL, explicit push-channel provenance, and download/cache generation.
type ChannelImageSource = tuple[str, bool, str | int | None]


@dataclass(frozen=True, slots=True)
class PushedChannelImage:
    """A lens-tagged image and the poll watermark when it arrived."""

    url: str = field(repr=False)
    revision: int
    source_time: datetime | None
    poll_time: datetime | None


def _image_time(value: object, zone: tzinfo | None) -> datetime | None:
    """Normalize ISO and epoch event times without guessing an unknown zone."""
    try:
        if isinstance(value, str) and not value.isdecimal():
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None and zone is not None:
                stamp = stamp.replace(tzinfo=zone)
            return stamp if stamp.tzinfo is None else stamp.astimezone(UTC)
        if type(value) is int or isinstance(value, str):
            epoch = int(value)
            if epoch > 0:
                return datetime.fromtimestamp(
                    epoch / 1000 if epoch >= 10**12 else epoch, UTC
                )
    except ValueError, OverflowError, OSError:
        pass
    return None


def _comparable(first: datetime | None, second: datetime | None) -> bool:
    return (
        first is not None
        and second is not None
        and (first.tzinfo is None) == (second.tzinfo is None)
    )


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
        self.intercom_events = IntercomEvents(self)
        self._local_devices: dict[str, LocalDevice] = {}
        self._local_rtsp_coordinators: dict[str, LocalRtspCoordinator] = {}
        self._update_lock = asyncio.Lock()
        self._poll_future: asyncio.Future[DeviceSnapshot] | None = None
        self.inventory_snapshot: DeviceSnapshot | None = None
        self.inventory_data: dict[str, dict[str, Any]] = {}
        self._closing = False
        self._channel_image_batches: OrderedDict[tuple[Any, ...], dict[str, bytes]] = (
            OrderedDict()
        )
        self._image_downloads: dict[tuple[Any, ...], asyncio.Task] = {}
        self.image_records: OrderedDict[str, tuple[str, str] | tuple[str, str, str]] = (
            OrderedDict()
        )
        self.channel_images: dict[tuple[str, str], PushedChannelImage] = {}
        self._image_zones: dict[str, tzinfo] = {}
        self._channel_image_revision = 0
        self._channel_notification_ids: OrderedDict[tuple[str, str, str], None] = (
            OrderedDict()
        )
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
            inventory, cameras, zones = await self.async_execute(
                _project_snapshot, result
            )
            self.inventory_snapshot = result
            self.inventory_data = inventory
            self._image_zones = zones
            valid_channels = {
                (serial, channel.key)
                for serial, data in cameras.items()
                for channel in camera_channels(data)
            }
            self.channel_images = {
                key: value
                for key, value in self.channel_images.items()
                if key in valid_channels
            }
            self.intercom_events.snapshot(result, zones)

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
            DevicePollOptions(timeout=self._api_timeout, include_intercom_events=True)
        )

    def register_image(
        self, serial: str, url: str, channel_key: str | None = None
    ) -> str:
        """Remember only SDK-provided images in a bounded account-local registry."""
        if serial not in (self.data or {}):
            raise ValueError("Unknown camera")
        if channel_key is not None:
            resolve_camera_channel(self.data[serial], channel_key)
        record = (serial, url) if channel_key is None else (serial, url, channel_key)
        for reference, existing in self.image_records.items():
            if existing == record:
                return reference
        reference = token_urlsafe(24)
        self.image_records[reference] = record
        while len(self.image_records) > 1024:
            self.image_records.popitem(last=False)
        return reference

    def local_device(self, serial: str) -> LocalDevice:
        """Share one local execution lock across a camera's local control families."""
        if serial not in self._local_devices:
            self._local_devices[serial] = LocalDevice(self, serial)
        return self._local_devices[serial]

    def local_rtsp_coordinator(self, serial: str) -> LocalRtspCoordinator:
        """Share one observed RTSP state owner between entities and options flows."""
        if self._closing:
            raise HomeAssistantError("EZVIZ coordinator is shutting down")
        if serial not in self._local_rtsp_coordinators:
            self._local_rtsp_coordinators[serial] = LocalRtspCoordinator(self, serial)
        return self._local_rtsp_coordinators[serial]

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

    async def async_channel_image_batch(
        self,
        key: tuple[Any, ...],
        operation: Callable[[], Coroutine[Any, Any, tuple[dict[str, bytes], bool]]],
    ) -> dict[str, bytes]:
        """Retain verified batches, including those missing the requested lens."""
        if key in self._channel_image_batches:
            self._channel_image_batches.move_to_end(key)
            return self._channel_image_batches[key]
        result, cacheable = await self.async_image_download(key, operation)
        if (
            not self.shutting_down
            and cacheable
            and sum(map(len, result.values())) <= 4 * 1024 * 1024
        ):
            self._channel_image_batches[key] = result
            if len(self._channel_image_batches) > 8:
                self._channel_image_batches.popitem(last=False)
        return result

    async def async_image_download(
        self, key: tuple[Any, ...], operation: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:
        """Share in-flight cloud image downloads and RTSP frame grabs."""
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
        self.intercom_events.close()
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
        for local in tuple(self._local_rtsp_coordinators.values()):
            await local.async_shutdown()
        pending = tuple(self._action_futures)
        if self._poll_future is not None:
            pending += (self._poll_future,)
        if pending:
            await asyncio.gather(
                *(asyncio.shield(future) for future in pending), return_exceptions=True
            )
        await self.hass.async_add_executor_job(self.ezviz_client.close_session)
        self._close_succeeded = True

    def channel_image_sources(
        self, serial: str, channel_key: str
    ) -> tuple[ChannelImageSource, ...]:
        """Order poll and push candidates; decoding still has to verify the lens."""
        data = self.inventory_data.get(serial, {})
        url, generation = data.get("last_alarm_pic"), data.get("last_alarm_time")
        polled: ChannelImageSource | None = None
        if isinstance(url, str) and url:
            polled = (
                url,
                False,
                generation if type(generation) in (str, int) else None,
            )
        pushed = self.channel_images.get((serial, channel_key))
        if pushed is None:
            return (polled,) if polled is not None else ()
        observed = (pushed.url, True, pushed.revision)
        if polled is None:
            return (observed,)

        poll_time = _image_time(polled[2], self._image_zones.get(serial))
        comparison = pushed.source_time
        if not _comparable(poll_time, comparison):
            # Without a comparable push timestamp, only an advancing poll can win.
            comparison = pushed.poll_time
        if poll_time is not None and (
            comparison is None
            or (_comparable(poll_time, comparison) and poll_time > comparison)
        ):
            return polled, observed
        return observed, polled

    def merge_mqtt_update(self, serial: str, mqtt_data: dict) -> None:
        """Merge MQTT update data into the coordinator."""

        # Push cannot invent inventory entries or mutate an earlier snapshot.
        if self._closing or not self.data or serial not in self.data:
            return
        ext = mqtt_data.get("ext")
        if not isinstance(ext, dict):
            return
        if ext.get("channel_type", 1) not in (1, "1"):
            return
        notification = camera_channel_notification(self.data[serial], mqtt_data)
        if notification is not None:
            key = (serial, notification.channel.key)
            already_seen = (
                notification.event_id is not None
                and (*key, notification.event_id) in self._channel_notification_ids
            )
            previous = self.channel_images.get(key)
            zone = self._image_zones.get(serial)
            source_time = _image_time(notification.source_time, zone)
            previous_time = previous.source_time if previous is not None else None
            older = (
                source_time is not None
                and previous_time is not None
                and _comparable(source_time, previous_time)
                and source_time < previous_time
            )
            if (
                notification.image_url is not None
                and not older
                and (
                    not already_seen
                    or previous is None
                    or previous.url != notification.image_url
                )
            ):
                self._channel_image_revision += 1
                self.channel_images[key] = PushedChannelImage(
                    notification.image_url,
                    self._channel_image_revision,
                    source_time,
                    _image_time(
                        self.inventory_data.get(serial, {}).get("last_alarm_time"),
                        zone,
                    ),
                )
            if notification.event_id is not None:
                event_key = (*key, notification.event_id)
                if event_key not in self._channel_notification_ids:
                    self._channel_notification_ids[event_key] = None
                    if len(self._channel_notification_ids) > 1024:
                        self._channel_notification_ids.popitem(last=False)
                    async_dispatcher_send(
                        self.hass,
                        "ezviz_cloud_channel_notification_{}_{}".format(*key),
                        notification,
                    )
        intercom_event = self.intercom_events.push(mqtt_data)
        if not ext.get("image"):
            return
        changes = {
            "last_alarm_type_code": ext.get("alert_type_code"),
            "last_alarm_time": ext.get("time"),
            "last_alarm_pic": ext.get("image"),
            "last_alarm_type_name": mqtt_data.get("alert"),
        }
        if (
            intercom_event is None
            and intercom_event_type(ext.get("alert_type_code")) is None
        ):
            changes["Motion_Trigger"] = True
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
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, tzinfo]]:
    """Thaw once off-loop, keeping SDK command adapters detached from inventory."""
    inventory = result.as_dict()
    cameras = {
        serial: deepcopy(inventory[serial]) for serial in result.select("camera")
    }
    return inventory, cameras, event_zones(inventory)


def _retrieve_worker_error(future: asyncio.Future) -> None:
    """Retrieve late failures without cancelling a still-owned executor job."""
    if not future.cancelled():
        future.exception()


class LocalRtspCoordinator(DataUpdateCoordinator[bool]):
    """Poll one opted-in camera independently of cloud health and refreshes."""

    def __init__(self, account: EzvizDataUpdateCoordinator, serial: str) -> None:
        """Initialize an account-owned local IO lifetime and per-camera lock."""
        super().__init__(
            account.hass,
            _LOGGER,
            config_entry=account.config_entry,
            name="EZVIZ local RTSP",
            update_interval=timedelta(minutes=5),
        )
        # Optional LAN setup runs in the background; no observation exists yet.
        self.last_update_success = False
        self.account = account
        self.serial = serial
        self.device = account.local_device(serial)

    @override
    async def _async_update_data(self) -> bool:
        try:
            return await self.device.async_execute(read_local_rtsp)
        except PyEzvizError, OSError, ValueError, TypeError, KeyError:
            # Never include device responses, addresses or credential-bearing errors.
            raise UpdateFailed("Local RTSP configuration unavailable") from None

    async def async_set_enabled(
        self,
        enabled: bool,
        *,
        options: Mapping[str, Any] | None = None,
        precondition: Callable[[], Any] | None = None,
    ) -> None:
        """Own the complete write/read-back even if the service caller cancels."""

        async def operation() -> None:
            try:
                observed = await self.device.async_execute(
                    write_local_rtsp,
                    enabled,
                    options=options,
                    precondition=precondition,
                )
            except PyEzvizError, OSError, ValueError, TypeError, KeyError:
                if not self.account.shutting_down:
                    self.async_set_update_error(
                        UpdateFailed("Local RTSP change refused or not verified")
                    )
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="local_rtsp_unverified"
                ) from None
            if not self.account.shutting_down:
                self.async_set_updated_data(observed)

        await self.account.async_execute_owned(operation)
