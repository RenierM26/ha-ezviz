"""EZVIZ integration init."""

from __future__ import annotations

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

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    ATTR_SERIAL,
    ATTR_TYPE_CAMERA,
    # Entry/data typing
    ATTR_TYPE_CLOUD,
    # Per-camera fields
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,  # per-camera only
    CONF_RF_SESSION_ID,
    CONF_RTSP_USES_VERIFICATION_CODE,
    # Token-based cloud fields
    CONF_SESSION_ID,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,
    # Options defaults
    DEFAULT_TIMEOUT,
    # Domain / keys
    DOMAIN,
    MQTT_HANDLER,
)
from .coordinator import EzvizDataUpdateCoordinator
from .mqtt import EzvizMqttHandler

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
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
]

OPTIONS_KEY_CAMERAS = "cameras"
FETCH = "fetch_my_key"
TARGET_VERSION = 4


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EZVIZ cloud entry."""
    hass.data.setdefault(DOMAIN, {})

    # Only cloud entries are supported; ignore legacy per-camera entries.
    if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
        return True

    # Ensure minimal default options exist (fresh dict)
    if not entry.options:
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_TIMEOUT: DEFAULT_TIMEOUT,
                OPTIONS_KEY_CAMERAS: {},
            },
        )

    # Must have stable token fields; otherwise kick reauth
    required_token_keys = (CONF_SESSION_ID, CONF_RF_SESSION_ID, CONF_URL, CONF_USER_ID)
    if not all(k in entry.data for k in required_token_keys):
        raise ConfigEntryAuthFailed("Need to reauthenticate to obtain EZVIZ tokens")

    # Build API client from tokens
    client = EzvizClient(
        token={
            CONF_SESSION_ID: entry.data[CONF_SESSION_ID],
            CONF_RF_SESSION_ID: entry.data[CONF_RF_SESSION_ID],
            "api_url": entry.data[CONF_URL],  # region URL is stable
            "username": entry.data[CONF_USER_ID],  # EZVIZ internal user id (MQTT)
        },
        timeout=entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )

    try:
        # Validate tokens; library will refresh them transparently
        token = await hass.async_add_executor_job(client.login)

    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        raise ConfigEntryAuthFailed from err

    except (InvalidURL, HTTPError, PyEzvizError) as err:
        raise ConfigEntryNotReady(f"Unable to connect to Ezviz service: {err}") from err

    # Persist only if the library rotated session tokens
    to_update: dict[str, Any] = {}
    sid = token[CONF_SESSION_ID]
    if sid and sid != entry.data[CONF_SESSION_ID]:
        to_update[CONF_SESSION_ID] = sid
    rf = token[CONF_RF_SESSION_ID]
    if rf and rf != entry.data[CONF_RF_SESSION_ID]:
        to_update[CONF_RF_SESSION_ID] = rf
    if to_update:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **to_update})

    # Coordinator (refresh for initial data)
    coordinator = EzvizDataUpdateCoordinator(
        hass,
        api=client,
        api_timeout=entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )
    await coordinator.async_config_entry_first_refresh()

    # MQTT
    mqtt_handler = EzvizMqttHandler(hass, client, entry.entry_id)

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        MQTT_HANDLER: mqtt_handler,
    }

    # Start MQTT
    await hass.async_add_executor_job(mqtt_handler.start)

    # Clean shutdown on HA stop (stop MQTT first)
    async def _shutdown(_event: Any) -> None:
        await hass.async_add_executor_job(mqtt_handler.stop)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)

    # Forward platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the EZVIZ cloud entry (stop MQTT first, then platforms)."""
    data = hass.data[DOMAIN][entry.entry_id]

    # 1) Stop MQTT first
    await hass.async_add_executor_job(data[MQTT_HANDLER].stop)

    # 2) Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # 3) Cleanup stored data
    if unload_ok:
        del hass.data[DOMAIN][entry.entry_id]

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate EZVIZ entries to v4 (no cross-domain handling)."""
    if entry.version >= TARGET_VERSION:
        return True

    _LOGGER.debug("Migrating entry %s from v%s", entry.entry_id, entry.version)
    etype = entry.data.get(CONF_TYPE)

    # Legacy per-camera entries: cloud migration absorbs & removes them; no-op here.
    if etype == ATTR_TYPE_CAMERA:
        return True

    if etype == ATTR_TYPE_CLOUD:
        # Minimal options: timeout + cameras
        prev_opts: dict[str, Any] = dict(entry.options or {})
        cameras_map: dict[str, Any] = dict(prev_opts.get(OPTIONS_KEY_CAMERAS, {}))
        new_options: dict[str, Any] = {
            CONF_TIMEOUT: prev_opts.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            OPTIONS_KEY_CAMERAS: cameras_map,
        }

        # Gather legacy camera entries in the same domain
        legacy_cam_entries = [
            e
            for e in hass.config_entries.async_entries(domain=DOMAIN)
            if e.entry_id != entry.entry_id
            and e.data.get(CONF_TYPE) == ATTR_TYPE_CAMERA
        ]

        # Strict consolidation: serial required, normalized, duplicates = error
        for cam_entry in legacy_cam_entries:
            serial = cam_entry.data[ATTR_SERIAL]  # strict: KeyError if missing

            if serial in cameras_map:
                raise ValueError(f"Duplicate camera serial during migration: {serial}")

            cameras_map[serial] = {
                CONF_USERNAME: cam_entry.data.get(
                    CONF_USERNAME, DEFAULT_CAMERA_USERNAME
                ),
                CONF_PASSWORD: cam_entry.data.get(CONF_PASSWORD, FETCH),
                CONF_ENC_KEY: cam_entry.data.get(CONF_ENC_KEY, FETCH),
                CONF_RTSP_USES_VERIFICATION_CODE: cam_entry.data.get(
                    CONF_RTSP_USES_VERIFICATION_CODE, False
                ),
                CONF_FFMPEG_ARGUMENTS: cam_entry.options.get(
                    CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                ),
            }

        # Persist cloud options and bump only the cloud entry
        hass.config_entries.async_update_entry(
            entry, options=new_options, version=TARGET_VERSION
        )

        # Remove legacy camera entries (same domain)
        for cam_entry in legacy_cam_entries:
            await hass.config_entries.async_remove(cam_entry.entry_id)

        _LOGGER.info("Migrated cloud entry %s to v%d", entry.entry_id, TARGET_VERSION)
        return True

    _LOGGER.warning("Entry %s has unexpected type %s", entry.entry_id, etype)
    return True
