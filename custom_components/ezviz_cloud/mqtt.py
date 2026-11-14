"""EZVIZ MQTT Handler."""

import asyncio
import contextlib
from copy import deepcopy
import logging

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    EzvizPushFatalError,
    EzvizTokenPersistenceError,
)
from pyezvizapi.mqtt import MQTTClient
from pyezvizapi.push.longlink.codec import AuthenticationRejected

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .workers import create_critical_task, create_executor_task

_LOGGER = logging.getLogger(__name__)

PUSH_RETRY_SECONDS = 300
PUSH_STOP_TIMEOUT_SECONDS = 5
PUSH_HEALTH_SECONDS = 5
# SDK 2.0.0 has no typed auth-fatal subtype and sometimes omits a cause.
# Match only its two explicit auth markers, never arbitrary fatal error text.
_AUTH_FATAL_MESSAGES = {
    "Channel-99 session expired; reauthentication required",
    "Channel-99 refresh rejected; reauthentication required",
}


class EzvizMqttHandler:
    """Wrapper for MQTT client to forward Ezviz push events into HA."""

    _coordinator: EzvizDataUpdateCoordinator

    def __init__(self, hass: HomeAssistant, client: EzvizClient, entry_id: str) -> None:
        """Initialize EZVIZ MQTT handler."""
        self._entry = entry_id
        self._hass = hass
        self._client = client
        self._mqtt: MQTTClient | None = None
        self._task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._worker_futures: set[asyncio.Future] = set()
        self._reauth_requested = False
        self._fatal_error_type: str | None = None
        self._fatal_failure_category: str | None = None
        self._stopping = asyncio.Event()

    def async_start(self) -> None:
        """Start optional push in the background without delaying entity setup."""
        if self._task is not None or self._stopping.is_set():
            return
        entry = self._hass.config_entries.async_get_entry(self._entry)
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            return
        self._coordinator = runtime.coordinator
        self._task = self._hass.async_create_background_task(
            self._async_connect(), "EZVIZ push startup"
        )

    async def _async_connect(self) -> None:
        """Retry push startup independently of the polling coordinator."""
        failed = False
        while not self._stopping.is_set():
            try:
                await self._async_run_worker(self.start)
            except Exception as err:  # noqa: BLE001 - Preserve ownership after unknown SDK failures.
                if await self._async_handle_terminal_failure(err):
                    return
                if not failed:
                    _LOGGER.warning(
                        "EZVIZ push unavailable (%s); continuing with polling. "
                        "Retrying in %s seconds",
                        type(err).__name__,
                        PUSH_RETRY_SECONDS,
                    )
                else:
                    _LOGGER.debug("EZVIZ push retry failed (%s)", type(err).__name__)
                failed = True
                # Also release any partially connected Paho client before retrying.
                await self._async_run_worker(self.stop)
            else:
                if failed and not self._stopping.is_set():
                    _LOGGER.info("EZVIZ push connection restored")
                if not self._stopping.is_set():
                    self._health_task = self._hass.async_create_background_task(
                        self._async_watch_health(), "EZVIZ push health"
                    )
                return

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), PUSH_RETRY_SECONDS)

    async def _async_watch_health(self) -> None:
        """A successful connect() is not proof its background worker stayed healthy."""
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), PUSH_HEALTH_SECONDS)
            except TimeoutError:
                pass
            else:
                return
            try:
                mqtt = self._mqtt
                if mqtt is None:
                    return
                await self._async_run_worker(mqtt.raise_if_failed)
            except Exception as err:  # noqa: BLE001 - Preserve ownership after unknown SDK failures.
                _LOGGER.warning(
                    "EZVIZ push worker failed (%s); polling remains active",
                    type(err).__name__,
                )
                if await self._async_handle_terminal_failure(err):
                    return
                if not self._stopping.is_set():
                    self._task = self._hass.async_create_background_task(
                        self._async_connect(), "EZVIZ push recovery"
                    )
                return

    async def _async_handle_terminal_failure(self, err: Exception) -> bool:
        """Do not reconnect a cached SDK worker retaining a fatal exception."""
        # Persistence failures are fatal too, but a login cannot fix failed writes.
        persistence_failed = isinstance(err, EzvizTokenPersistenceError)
        cause: BaseException | None = err
        seen: set[int] = set()
        auth_failed = (
            isinstance(err, EzvizPushFatalError) and str(err) in _AUTH_FATAL_MESSAGES
        )
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            persistence_failed |= isinstance(cause, EzvizTokenPersistenceError)
            auth_failed |= isinstance(
                cause,
                (
                    EzvizAuthTokenExpired,
                    EzvizAuthVerificationCode,
                    AuthenticationRejected,
                ),
            )
            cause = cause.__cause__
        auth_failed &= not persistence_failed
        if not isinstance(err, EzvizPushFatalError) and not auth_failed:
            return False
        self._fatal_error_type = type(err).__name__
        self._fatal_failure_category = (
            "authentication"
            if auth_failed
            else "storage"
            if persistence_failed
            else "other"
        )
        if auth_failed:
            if not self._reauth_requested:
                self._reauth_requested = True
                entry = self._hass.config_entries.async_get_entry(self._entry)
                if entry is not None:
                    entry.async_start_reauth(self._hass)
            recovery = "Reauthenticate the integration to restore push"
        elif persistence_failed:
            recovery = "Check Home Assistant storage permissions and free space, then reload the integration"
        else:
            recovery = "Inspect the SDK push failure, then reload the integration after correcting its cause"
        _LOGGER.error(
            "EZVIZ push stopped (%s); polling remains active. %s. Automatic push retries paused",
            self._fatal_error_type,
            recovery,
        )
        # A failed stop retains ownership for normal unload/shutdown cleanup.
        await self._async_run_worker(self.stop)
        return True

    async def async_stop(self) -> None:
        """Stop retries and bound the caller's wait for SDK cleanup."""
        self._stopping.set()
        if self._stop_task is None:
            self._stop_task = create_critical_task(
                self._hass, self._async_finish_stop(), "EZVIZ push cleanup"
            )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._stop_task), PUSH_STOP_TIMEOUT_SECONDS
            )
        except TimeoutError:
            _LOGGER.debug("EZVIZ push cleanup still pending; continuing shutdown")

    async def async_wait_stopped(self) -> None:
        """Resource owners can await full cleanup after the foreground timeout."""
        await self.async_stop()
        if self._stop_task is not None:
            await asyncio.shield(self._stop_task)

    async def _async_finish_stop(self) -> None:
        """Serialize cleanup after startup without blocking the unload caller."""
        monitors = tuple(
            task for task in (self._health_task, self._task) if task is not None
        )
        if monitors:
            await asyncio.gather(
                *(asyncio.shield(task) for task in monitors), return_exceptions=True
            )
        # HA may cancel the monitors first; their executor work still has owners.
        if self._worker_futures:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._worker_futures)),
                return_exceptions=True,
            )
        while not await self._async_run_worker(self.stop):
            await asyncio.sleep(PUSH_RETRY_SECONDS)

    async def _async_run_worker(self, func):
        future = create_executor_task(self._hass, func)
        self._worker_futures.add(future)
        future.add_done_callback(self._worker_done)
        return await asyncio.shield(future)

    def _worker_done(self, future):
        self._worker_futures.discard(future)
        if not future.cancelled():
            future.exception()

    def start(self) -> None:
        """Start MQTT listener (executor only)."""
        try:
            if self._mqtt is not None and not self.stop():
                raise RuntimeError(
                    "Previous EZVIZ push client cleanup is still pending"
                )
            self._mqtt = self._client.get_mqtt_client(
                on_message_callback=self._on_message
            )
            self._mqtt.connect()
            _LOGGER.debug("EZVIZ MQTT started")
        finally:
            # HA may cancel background tasks during shutdown, but cannot cancel
            # this worker. Clean up here too when a stalled SDK call returns.
            if self._stopping.is_set():
                self.stop()

    def stop(self) -> bool:
        """Best-effort cleanup; a push outage must not prevent entry unload."""
        mqtt = self._mqtt
        if mqtt is None:
            return True
        try:
            mqtt.stop()
        except Exception as err:  # noqa: BLE001 - Preserve ownership after unknown SDK failures.
            _LOGGER.debug("EZVIZ push cleanup failed (%s)", type(err).__name__)
            return False
        self._mqtt = None
        _LOGGER.debug("EZVIZ MQTT stopped")
        return True

    def get_diagnostics(self) -> dict:
        """Only credential-free worker fields; no full tokens or session payload."""
        mqtt = self._mqtt
        if mqtt is None:
            return {
                "state": "fatal" if self._fatal_error_type else "unavailable",
                "reauth_requested": self._reauth_requested,
                "fatal_error_type": self._fatal_error_type,
                "fatal_failure_category": self._fatal_failure_category,
            }
        allowed = {
            "state",
            "worker_alive",
            "retry_delay_seconds",
            "attempts",
            "transient_failures",
            "last_error_type",
            "fatal_error_type",
        }
        return {
            **{k: v for k, v in mqtt.get_push_diagnostics().items() if k in allowed},
            "reauth_requested": self._reauth_requested,
            "fatal_failure_category": self._fatal_failure_category,
        }

    def _on_message(self, event: dict) -> None:
        """Handle incoming MQTT push message (called from MQTT thread)."""

        if self._stopping.is_set() or not isinstance(event, dict):
            return
        ext = event.get("ext")
        if (
            not isinstance(ext, dict)
            or not isinstance(ext.get("device_serial"), str)
            or not ext.get("device_serial")
        ):
            return
        event = deepcopy(event)

        def _handle() -> None:
            """Handle incoming MQTT push message."""
            if self._stopping.is_set():
                return
            serial = event["ext"]["device_serial"]
            ha_device_id = None

            # Access device registry
            device_registry = dr.async_get(self._hass)

            # Look up the device by identifiers (DOMAIN, serial)
            device = device_registry.async_get_device_by_identifier(
                (DOMAIN, serial), self._entry
            )
            if device:
                ha_device_id = device.id

            # Add device ID to event
            event["device_id"] = ha_device_id

            _LOGGER.debug(
                "MQTT push: serial=%s resolved device_id=%s",
                serial,
                ha_device_id,
            )

            # Merge event data into coordinator
            self._coordinator.merge_mqtt_update(serial, event)

            # Fire HA event
            self._hass.bus.async_fire("ezviz_push_event", event)

        # Schedule on HA event loop
        self._hass.loop.call_soon_threadsafe(_handle)
