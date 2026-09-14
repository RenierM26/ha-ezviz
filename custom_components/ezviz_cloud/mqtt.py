"""EZVIZ MQTT Handler."""

import asyncio
import contextlib
import logging

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizPushFatalError,
    EzvizTokenPersistenceError,
)
from pyezvizapi.mqtt import MQTTClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .const import DOMAIN
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PUSH_STOP_TIMEOUT_SECONDS = 5
PUSH_HEALTH_SECONDS = 5
PUSH_CLEANUP_RETRY_SECONDS = 1


class EzvizMqttHandler:
    """Wrapper for MQTT client to forward Ezviz push events into HA."""

    _coordinator: EzvizDataUpdateCoordinator

    def __init__(self, hass: HomeAssistant, client: EzvizClient, entry: ConfigEntry,
                 coordinator: EzvizDataUpdateCoordinator) -> None:
        """Initialize EZVIZ MQTT handler."""
        self._coordinator = coordinator
        self._entry = entry.entry_id
        self._config_entry = entry
        self._hass = hass
        self._client = client
        self._mqtt: MQTTClient | None = None
        self._task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._startup: asyncio.Future | None = None
        self._stopping = asyncio.Event()

    def async_start(self) -> None:
        """Start optional push in the background without delaying entity setup."""
        if self._task is not None or self._stopping.is_set():
            return
        self._task = self._config_entry.async_create_background_task(
            self._hass, self._async_connect(), "EZVIZ push monitor"
        )

    async def _async_connect(self) -> None:
        """Start once; the SDK owns transient failures and reconnection."""
        # Stop/unload may have completed while this monitor was still queued.
        # No await between this check and publishing startup ownership below.
        if self._stopping.is_set():
            return
        try:
            self._startup = asyncio.create_task(self._async_start(), name="EZVIZ SDK startup")
            # Cancelling the monitor must not lose ownership of executor startup.
            await asyncio.shield(self._startup)
            while not self._stopping.is_set():
                self._check_health()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), PUSH_HEALTH_SECONDS)
        except EzvizTokenPersistenceError:
            ir.async_create_issue(
                self._hass, DOMAIN, f"push_storage_{self._entry}",
                is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key="push_storage",
            )
            _LOGGER.error("EZVIZ push stopped because credential storage failed")
        except (EzvizAuthTokenExpired, EzvizPushFatalError):
            _LOGGER.warning("EZVIZ push requires reauthentication; polling continues")
            self._config_entry.async_start_reauth(self._hass)
        except Exception as err:
            _LOGGER.error("EZVIZ push could not start (%s); polling continues", type(err).__name__)
        finally:
            self._stopping.set()
            await asyncio.shield(self._ensure_cleanup())

    async def _async_start(self) -> None:
        # HA cancels executor futures submitted from a background task directly,
        # even when shielded. Own this bootstrap future until cleanup completes.
        # Network registration/retries are still owned by the SDK worker.
        await asyncio.to_thread(self.start)

    def _ensure_cleanup(self) -> asyncio.Task:
        """One cleanup owner, independent of cancellation of the monitor."""
        if self._stop_task is None or self._stop_task.cancelled():
            # Not a background task: HA must allow cleanup during shutdown.
            self._stop_task = self._hass.async_create_task(
                self._async_finish_stop(), "EZVIZ push cleanup"
            )
        return self._stop_task

    async def async_stop(self) -> bool:
        """Stop monitoring and bound the caller's wait for SDK cleanup."""
        self._stopping.set()
        cleanup = self._ensure_cleanup()
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup), PUSH_STOP_TIMEOUT_SECONDS
            )
        except TimeoutError:
            _LOGGER.debug("EZVIZ push cleanup still pending; retry unloading later")
            return False
        return True

    def _check_health(self) -> None:
        """Surface fatal worker errors without stopping the polling coordinator."""
        if self._mqtt is not None:
            self._mqtt.raise_if_failed()

    async def _async_finish_stop(self) -> None:
        """Serialize cleanup after startup without blocking the unload caller."""
        if self._startup is not None:
            # Startup exceptions are reported by the monitor. Still release any
            # partially-created client; never await the monitor from its cleanup.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._startup)
        await self._async_cleanup()

    async def _async_cleanup(self) -> None:
        """Keep ownership and retry cleanup after fatal errors or unload."""
        while not await self._hass.async_add_executor_job(self.stop):
            await asyncio.sleep(PUSH_CLEANUP_RETRY_SECONDS)

    def start(self) -> None:
        """Start MQTT listener (executor only)."""
        try:
            if self._mqtt is not None and not self.stop():
                raise RuntimeError("Previous EZVIZ push client cleanup is still pending")
            self._mqtt = self._client.get_mqtt_client(on_message_callback=self._on_message)
            self._mqtt.connect()
            _LOGGER.debug("EZVIZ MQTT started")
        finally:
            # HA may cancel background tasks during shutdown, but cannot cancel
            # this worker. Clean up here too when a stalled SDK call returns.
            if self._stopping.is_set():
                self.stop()

    def stop(self) -> bool:
        """Retain the client until cleanup succeeds so reload cannot overlap it."""
        mqtt = self._mqtt
        if mqtt is None:
            return True
        try:
            mqtt.stop()
        except Exception as err:
            _LOGGER.debug("EZVIZ push cleanup failed (%s)", type(err).__name__)
            return False
        self._mqtt = None
        _LOGGER.debug("EZVIZ MQTT stopped")
        return True

    def _on_message(self, event: dict) -> None:
        """Handle incoming MQTT push message (called from MQTT thread)."""

        def _handle() -> None:
            """Handle incoming MQTT push message."""
            if self._stopping.is_set():
                return
            serial = event["ext"]["device_serial"]
            ha_device_id = None

            # Access device registry
            device_registry = dr.async_get(self._hass)

            # Look up the device by identifiers (DOMAIN, serial)
            device = device_registry.async_get_device({(DOMAIN, serial)})
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
