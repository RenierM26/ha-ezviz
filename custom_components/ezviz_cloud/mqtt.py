"""EZVIZ MQTT Handler."""

import logging

from pyezvizapi.client import EzvizClient
from pyezvizapi.mqtt import MQTTClient

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class EzvizMqttHandler:
    """Wrapper for MQTT client to forward Ezviz push events into HA."""

    _coordinator: EzvizDataUpdateCoordinator

    def __init__(self, hass: HomeAssistant, client: EzvizClient, entry_id: str) -> None:
        """Initialize EZVIZ MQTT handler."""
        self._entry = entry_id
        self._hass = hass
        self._mqtt: MQTTClient = client.get_mqtt_client(
            on_message_callback=self._on_message
        )

    def start(self) -> None:
        """Start MQTT listener."""
        self._mqtt.connect()
        self._coordinator: EzvizDataUpdateCoordinator = self._hass.data[DOMAIN][
            self._entry
        ][DATA_COORDINATOR]
        _LOGGER.debug("EZVIZ MQTT started")

    def stop(self) -> None:
        """Stop MQTT listener."""
        self._mqtt.stop()
        _LOGGER.debug("EZVIZ MQTT stopped")

    def _on_message(self, event: dict) -> None:
        """Handle incoming MQTT push message (called from MQTT thread)."""

        def _handle() -> None:
            """Handle incoming MQTT push message."""
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
