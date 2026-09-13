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

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PUSH_RETRY_SECONDS = 300
PUSH_STOP_TIMEOUT_SECONDS = 5
PUSH_HEALTH_SECONDS = 5


class EzvizMqttHandler:
    """Wrapper for MQTT client to forward Ezviz push events into HA."""

    _coordinator: EzvizDataUpdateCoordinator

    def __init__(self, hass: HomeAssistant, client: EzvizClient, entry: ConfigEntry) -> None:
        """Initialize EZVIZ MQTT handler."""
        self._entry = entry.entry_id
        self._config_entry = entry
        self._hass = hass
        self._client = client
        self._mqtt: MQTTClient | None = None
        self._task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def async_start(self) -> None:
        """Start optional push in the background without delaying entity setup."""
        if self._task is not None or self._stopping.is_set():
            return
        self._coordinator = self._hass.data[DOMAIN][self._entry][DATA_COORDINATOR]
        self._task = self._hass.async_create_background_task(
            self._async_connect(), "EZVIZ push startup"
        )

    async def _async_connect(self) -> None:
        """Retry push startup independently of the polling coordinator."""
        failed = False
        while not self._stopping.is_set():
            try:
                await self._hass.async_add_executor_job(self.start)
                while not self._stopping.is_set():
                    await self._hass.async_add_executor_job(self._check_health)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), PUSH_HEALTH_SECONDS)
                return
            except EzvizTokenPersistenceError:
                ir.async_create_issue(
                    self._hass, DOMAIN, f"push_storage_{self._entry}",
                    is_fixable=False, severity=ir.IssueSeverity.ERROR,
                    translation_key="push_storage",
                )
                _LOGGER.error("EZVIZ push stopped because credential storage failed")
                await self._hass.async_add_executor_job(self.stop)
                return
            except (EzvizAuthTokenExpired, EzvizPushFatalError):
                _LOGGER.warning("EZVIZ push requires reauthentication; polling continues")
                self._config_entry.async_start_reauth(self._hass)
                await self._hass.async_add_executor_job(self.stop)
                return
            except Exception as err:  # Push is optional, including SDK failures.
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
                await self._hass.async_add_executor_job(self.stop)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), PUSH_RETRY_SECONDS)

    async def async_stop(self) -> bool:
        """Stop retries and bound the caller's wait for SDK cleanup."""
        self._stopping.set()
        if self._stop_task is None:
            self._stop_task = self._hass.async_create_background_task(
                self._async_finish_stop(), "EZVIZ push cleanup"
            )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._stop_task), PUSH_STOP_TIMEOUT_SECONDS
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
        if self._task is not None:
            # A timeout must not cancel the executor operation or race its cleanup.
            await asyncio.shield(self._task)
        while not await self._hass.async_add_executor_job(self.stop):
            await asyncio.sleep(1)

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
