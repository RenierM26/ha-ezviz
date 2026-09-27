"""EZVIZ integration init."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

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
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import (
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_TOKEN,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
    SETUP_DIAGNOSTICS,
)
from .coordinator import EzvizDataUpdateCoordinator
from .mqtt import EzvizMqttHandler
from .runtime import EzvizConfigEntry, EzvizRuntimeData
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


def _record_setup_failure(
    hass: HomeAssistant, entry: ConfigEntry, stage: str, error: BaseException
) -> None:
    """Keep a credential-free failure summary available while setup is retried."""
    hass.data.setdefault(DOMAIN, {}).setdefault(SETUP_DIAGNOSTICS, {})[
        entry.entry_id
    ] = {
        "state": "failed",
        "stage": stage,
        "error_type": type(error).__name__,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


async def async_setup_entry(  # noqa: PLR0915
    hass: HomeAssistant, entry: EzvizConfigEntry
) -> bool:
    """Set up EZVIZ Cloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Only handle cloud entries here
    if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
        return True

    # Web-profile tokens cannot be reused for the Android push profile.
    if not isinstance(entry.data.get(CONF_TOKEN), dict):
        error = ConfigEntryAuthFailed("Sign in again to migrate EZVIZ push credentials")
        _record_setup_failure(hass, entry, "credential_validation", error)
        raise error

    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    client = None
    coordinator = None
    setup_complete = False
    setup_stage = "credential_storage"
    try:
        token_store = EzvizTokenStore(hass, entry)
        token = await token_store.async_load()
        setup_stage = "authentication"
        client = EzvizClient(
            token=token, timeout=timeout, on_token_updated=token_store.save
        )
        await hass.async_add_executor_job(client.login)
        setup_stage = "initial_refresh"
        coordinator = EzvizDataUpdateCoordinator(hass, api=client, api_timeout=timeout)
        await coordinator.async_config_entry_first_refresh()

        mqtt_handler = EzvizMqttHandler(hass, client, entry, coordinator)
        entry.runtime_data = EzvizRuntimeData(client, coordinator, mqtt_handler, token_store)

        domain_data = hass.data.setdefault(DOMAIN, {})
        if not domain_data.get("_http_view_registered"):
            hass.http.register_view(ImageProxyView(hass))
            domain_data["_http_view_registered"] = True

        setup_stage = "platform_setup"
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # HA cancels monitors before this event. This awaited handler anchors
        # shared cleanup in the integration-stop phase, not the earlier task set.
        async def shutdown(_event: Event) -> None:
            # Quiesce polling before push/token cleanup. Read the current handler
            # in case a failed platform unload required restarting push.
            data = entry.runtime_data
            await data.coordinator.async_shutdown()
            if await data.push.async_stop():
                await hass.async_add_executor_job(data.client.close_session)

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
        ir.async_delete_issue(hass, DOMAIN, f"push_storage_{entry.entry_id}")
        domain_data.setdefault(SETUP_DIAGNOSTICS, {}).pop(entry.entry_id, None)
        mqtt_handler.async_start()
        setup_complete = True
        return True
    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        _record_setup_failure(hass, entry, setup_stage, err)
        raise ConfigEntryAuthFailed from err
    except (InvalidURL, HTTPError, PyEzvizError, OSError) as err:
        _record_setup_failure(hass, entry, setup_stage, err)
        raise ConfigEntryNotReady(
            f"Unable to initialize EZVIZ ({type(err).__name__})"
        ) from err
    except Exception as err:
        _record_setup_failure(hass, entry, setup_stage, err)
        raise
    finally:
        if not setup_complete:
            if coordinator is not None:
                await coordinator.async_shutdown()
            if client is not None:
                await hass.async_add_executor_job(client.close_session)
            if hasattr(entry, "runtime_data"):
                del entry.runtime_data


async def async_unload_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:
    """Do not allow replacement until the old SDK worker has exited."""
    data = getattr(entry, "runtime_data", None)
    if data is not None and not await data.push.async_stop():
        return False

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and data is not None:
        await data.coordinator.async_shutdown()
        await hass.async_add_executor_job(data.client.close_session)
        del entry.runtime_data
    elif not unload_ok and data is not None:
        # HA keeps the entry loaded on failure. Restore optional push only after
        # the previous worker has demonstrably stopped.
        data.push = EzvizMqttHandler(hass, data.client, entry, data.coordinator)
        data.push.async_start()
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
