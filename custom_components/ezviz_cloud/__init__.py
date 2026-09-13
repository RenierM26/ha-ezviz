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

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import (
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_TOKEN,
    DATA_COORDINATOR,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MQTT_HANDLER,
    OPTIONS_KEY_CAMERAS,
)
from .coordinator import EzvizDataUpdateCoordinator
from .mqtt import EzvizMqttHandler
from .token_store import EzvizTokenStore
from .views import ImageProxyView

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

TARGET_VERSION = 4


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EZVIZ Cloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Only handle cloud entries here
    if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
        return True

    # Web-profile tokens cannot be reused for the Android push profile.
    if not isinstance(entry.data.get(CONF_TOKEN), dict):
        raise ConfigEntryAuthFailed("Sign in again to migrate EZVIZ push credentials")

    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    token_store = EzvizTokenStore(hass, entry)
    client = None
    try:
        token = await token_store.async_load()
        client = EzvizClient(
            token=token, timeout=timeout, on_token_updated=token_store.save
        )
        await hass.async_add_executor_job(client.login)
    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        if client is not None:
            await hass.async_add_executor_job(client.close_session)
        raise ConfigEntryAuthFailed from err
    except (InvalidURL, HTTPError, PyEzvizError, OSError) as err:
        if client is not None:
            await hass.async_add_executor_job(client.close_session)
        raise ConfigEntryNotReady(
            f"Unable to initialize EZVIZ ({type(err).__name__})"
        ) from err

    ir.async_delete_issue(hass, DOMAIN, f"push_storage_{entry.entry_id}")

    # Coordinator
    coordinator = EzvizDataUpdateCoordinator(
        hass,
        api=client,
        api_timeout=timeout,
    )
    await coordinator.async_config_entry_first_refresh()

    # MQTT handler
    mqtt_handler = EzvizMqttHandler(hass, client, entry)

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        MQTT_HANDLER: mqtt_handler,
    }

    # Clean shutdown on HA stop (stop MQTT first)
    async def _shutdown(_event: Any) -> None:
        await mqtt_handler.async_stop()

    remove_shutdown = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    entry.async_on_unload(remove_shutdown)

    # Register HTTP view for image proxy/decryption once per instance
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("_http_view_registered"):
        hass.http.register_view(ImageProxyView(hass))
        domain_data["_http_view_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    mqtt_handler.async_start()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the EZVIZ cloud entry (stop MQTT first, then platforms)."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if data and (mqtt := data.get(MQTT_HANDLER)) and not await mqtt.async_stop():
        return False

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok and data:
        await hass.async_add_executor_job(data[DATA_COORDINATOR].ezviz_client.close_session)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove this account's durable credentials and repair issue."""
    await EzvizTokenStore.async_remove(hass, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, f"push_storage_{entry.entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entry to the current version."""
    if entry.version >= TARGET_VERSION:
        return True

    _LOGGER.debug("Migrating entry %s from v%s", entry.entry_id, entry.version)
    etype = entry.data.get(CONF_TYPE)
    if etype == ATTR_TYPE_CAMERA:
        # Per-camera placeholders will be removed by the cloud migration
        return True
    if etype != ATTR_TYPE_CLOUD:
        return True

    # Consolidate legacy camera entries into cloud options
    prev_opts = dict(entry.options or {})
    cameras_map = dict(prev_opts.get(OPTIONS_KEY_CAMERAS, {}))
    timeout_val = prev_opts.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    legacy_cams = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.data.get(CONF_TYPE) == ATTR_TYPE_CAMERA
    ]
    for cam in legacy_cams:
        serial = cam.unique_id  # strict
        if serial in cameras_map:
            _LOGGER.warning(
                "Skipping duplicate camera serial during migration: %s", serial
            )
            continue
        cameras_map[serial] = {
            CONF_USERNAME: cam.data.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME),
            CONF_PASSWORD: cam.data.get(CONF_PASSWORD, DEFAULT_FETCH_MY_KEY),
            CONF_ENC_KEY: cam.data.get(CONF_ENC_KEY, DEFAULT_FETCH_MY_KEY),
            CONF_RTSP_USES_VERIFICATION_CODE: cam.data.get(
                CONF_RTSP_USES_VERIFICATION_CODE, False
            ),
            CONF_FFMPEG_ARGUMENTS: cam.options.get(
                CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
            ),
        }

    hass.config_entries.async_update_entry(
        entry,
        options={CONF_TIMEOUT: timeout_val, OPTIONS_KEY_CAMERAS: cameras_map},
        version=TARGET_VERSION,
        minor_version=entry.minor_version,
    )

    # Strict purge: only entries with explicit version < 4
    victims = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
        and e.version < TARGET_VERSION
        and (e.source == SOURCE_IGNORE or e.data.get(CONF_TYPE) == ATTR_TYPE_CAMERA)
    ]
    for v in victims:
        try:
            await hass.config_entries.async_remove(v.entry_id)
        except Exception:
            _LOGGER.exception(
                "Failed to remove legacy entry %s during migration", v.entry_id
            )

    _LOGGER.info("Migrated EZVIZ cloud entry %s to v%d", entry.entry_id, TARGET_VERSION)
    return True
