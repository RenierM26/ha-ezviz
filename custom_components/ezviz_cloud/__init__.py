"""Support for EZVIZ camera."""

import logging

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)
from pyezvizapi.mqtt import MQTTClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RF_SESSION_ID,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_SESSION_ID,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS_BY_TYPE: dict[str, list] = {
    ATTR_TYPE_CAMERA: [],
    ATTR_TYPE_CLOUD: [
        Platform.ALARM_CONTROL_PANEL,
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.CAMERA,
        Platform.IMAGE,
        Platform.LIGHT,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SENSOR,
        Platform.SIREN,
        Platform.SWITCH,
        Platform.TEXT,
        Platform.UPDATE,
    ],
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EZVIZ from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    sensor_type: str = entry.data[CONF_TYPE]
    ezviz_client = None

    if not entry.options:
        options = {
            CONF_FFMPEG_ARGUMENTS: DEFAULT_FFMPEG_ARGUMENTS,
            CONF_TIMEOUT: DEFAULT_TIMEOUT,
        }

        hass.config_entries.async_update_entry(entry, options=options)

    # Initialize EZVIZ cloud entities
    if PLATFORMS_BY_TYPE[sensor_type]:

        # Reauth if user_id or session_id is missing
        if not entry.data.get(CONF_USER_ID) or not entry.data.get(CONF_SESSION_ID):
            raise ConfigEntryAuthFailed("Need to reauthenticate")

        ezviz_client = EzvizClient(
            token={
                CONF_SESSION_ID: entry.data[CONF_SESSION_ID],
                CONF_RF_SESSION_ID: entry.data[CONF_RF_SESSION_ID],
                "api_url": entry.data[CONF_URL],
                "username": entry.data[CONF_USER_ID],
            },
            timeout=entry.options[CONF_TIMEOUT],
        )

        try:
            await hass.async_add_executor_job(ezviz_client.login)

        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error

        except (InvalidURL, HTTPError, PyEzvizError) as error:
            raise ConfigEntryNotReady(
                f"Unable to connect to Ezviz service: {error}"
            ) from error

        mqtt_handler = EzvizMqttHandler(hass, ezviz_client)

        await hass.async_add_executor_job(mqtt_handler.start)

        coordinator = EzvizDataUpdateCoordinator(
            hass, api=ezviz_client, api_timeout=entry.options[CONF_TIMEOUT]
        )

        await coordinator.async_config_entry_first_refresh()

        hass.data[DOMAIN][entry.entry_id] = {DATA_COORDINATOR: coordinator, "mqtt": mqtt_handler}

    # Check EZVIZ cloud account entity is present, reload cloud account entities for camera entity change to take effect.
    # Cameras are accessed via local RTSP stream with unique credentials per camera.
    # Separate camera entities allow for credential changes per camera.
    if sensor_type == ATTR_TYPE_CAMERA and hass.data[DOMAIN]:
        for item in hass.config_entries.async_entries(
            domain=DOMAIN, include_ignore=False
        ):
            if item.data[CONF_TYPE] == ATTR_TYPE_CLOUD:
                _LOGGER.debug("Reload Ezviz main account with camera entry")
                await hass.config_entries.async_reload(item.entry_id)
                return True

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS_BY_TYPE[sensor_type]
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    sensor_type = entry.data[CONF_TYPE]

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_BY_TYPE[sensor_type]
    )
    if sensor_type == ATTR_TYPE_CLOUD and unload_ok:
        await hass.async_add_executor_job(hass.data[DOMAIN][entry.entry_id]["mqtt"].stop)
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old camera entry."""
    _LOGGER.debug("Migrating from version %s.%s", entry.version, entry.minor_version)

    if entry.version <= 2:
        if entry.data[CONF_TYPE] == ATTR_TYPE_CAMERA:
            data = {**entry.data}
            data[CONF_RTSP_USES_VERIFICATION_CODE] = True
            if not data.get(CONF_ENC_KEY):
                data[CONF_ENC_KEY] = data[CONF_PASSWORD]

            hass.config_entries.async_update_entry(entry, data=data, version=3)

        if entry.data[CONF_TYPE] == ATTR_TYPE_CLOUD:
            hass.config_entries.async_update_entry(entry, data=entry.data, version=3)

        _LOGGER.info(
            "Migration to version %s.%s successful for %s account",
            entry.version,
            entry.minor_version,
            entry.data[CONF_TYPE],
        )

    return True

class EzvizMqttHandler:
    """Wrapper for MQTT client to forward Ezviz push events into HA."""

    def __init__(self, hass: HomeAssistant, client: EzvizClient) -> None:
        self._hass = hass
        self._mqtt: MQTTClient | None = client.get_mqtt_client(on_message_callback=self._on_message)

    def start(self) -> None:
        """Start MQTT listener."""
        self._mqtt.connect()
        _LOGGER.debug("EZVIZ MQTT started")

    def stop(self) -> None:
        """Stop MQTT listener."""
        if self._mqtt:
            self._mqtt.stop()
            self._mqtt = None
            _LOGGER.debug("EZVIZ MQTT stopped")

    def _on_message(self, event: dict) -> None:
        """Handle incoming MQTT push message (called from MQTT thread)."""

        def _handle():
            # Fire HA event (optional for automations)
            serial = event["ext"]["device_serial"]

            self._hass.bus.async_fire("ezviz_push_event", {
                "device_serial": serial,
                "event": event
            })

            async_dispatcher_send(self._hass, f"{DOMAIN}_event_{serial}", event)

        # Ensure everything runs inside HA event loop
        self._hass.loop.call_soon_threadsafe(_handle)
