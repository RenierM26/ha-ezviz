"""EZVIZ integration init."""

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

from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady

from .const import (
    ATTR_TYPE_CAMERA,
    ATTR_TYPE_CLOUD,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_RTSP_USES_VERIFICATION_CODE,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
)
from .coordinator import EzvizDataUpdateCoordinator
from .mqtt import EzvizMqttHandler
from .repairs import clear_migration_issue, create_migration_issue
from .runtime import EzvizConfigEntry, RuntimeCleanup
from .tokens import TokenPersistence
from .views import ImageProxyView
from .workers import create_critical_task

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

TARGET_VERSION = 5


async def _async_validate_login(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    client: EzvizClient,
    persistence: TokenPersistence,
    runtime: RuntimeCleanup,
) -> None:
    """Validate session and persist rotation before platforms or push start."""
    try:
        await runtime.async_run(client.login)
        await runtime.async_run(persistence.save_from_worker, client.export_token())
    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        await runtime.async_stop()
        raise ConfigEntryAuthFailed from err
    except (InvalidURL, HTTPError, PyEzvizError) as err:
        await runtime.async_stop()
        raise ConfigEntryNotReady(
            f"Unable to connect to Ezviz service ({type(err).__name__})"
        ) from err
    except Exception as err:
        await runtime.async_stop()
        raise ConfigEntryNotReady(f"EZVIZ login failed ({type(err).__name__})") from err

    except BaseException:
        await runtime.async_stop()
        raise


def _create_token_client(token: dict[str, Any], timeout: int, persistence: TokenPersistence) -> EzvizClient:
    """Classify SDK construction failures before a runtime/session owner exists."""
    try:
        return EzvizClient(token=token, timeout=timeout, on_token_updated=persistence.save_from_worker)
    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        # The SDK validates host-bound tokens before allocating its HTTP session.
        # This boundary must request reauth too, not escape as an unexpected setup
        # error. Never rewrite a feature code or discard saved push state to bypass it.
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="saved_login_invalid"
        ) from err
    except (InvalidURL, HTTPError, PyEzvizError, OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"Unable to initialize the EZVIZ client ({type(err).__name__})"
        ) from err



async def async_setup_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:
    """Set up EZVIZ Cloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Only handle cloud entries here
    if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
        return True

    # Cleanup ownership must outlive HA clearing runtime_data after unload/failure.
    # Platforms and request handlers use entry.runtime_data, never this registry.
    runtimes = hass.data[DOMAIN].setdefault("_runtimes", {})
    previous = runtimes.get(entry.entry_id)
    if previous is not None and not previous.cleanup_complete:
        # A done task may have failed before releasing its resources. Retry the
        # same owner; never replace it with a new client while cleanup is uncertain.
        if previous.task is not None and previous.task.done():
            try:
                await previous.async_stop()
            except Exception as err:
                raise ConfigEntryNotReady(f"Previous EZVIZ cleanup failed ({type(err).__name__})") from err
        if not previous.cleanup_complete:
            raise ConfigEntryNotReady("Previous EZVIZ workers are still shutting down")
    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    persistence = TokenPersistence(hass, entry)
    try:
        token_input = await persistence.async_load()
    except OSError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="credential_storage_failed"
        ) from err
    if not token_input or not all(
        token_input.get(key) for key in ("session_id", "rf_session_id", "api_url", "username")
    ):
        raise ConfigEntryAuthFailed("Missing EZVIZ token fields; reauthenticate required")
    client = _create_token_client(token_input, timeout, persistence)

    coordinator = EzvizDataUpdateCoordinator(
        hass, config_entry=entry, api=client, api_timeout=timeout
    )
    runtime = RuntimeCleanup(hass, coordinator, persistence=persistence)
    runtimes[entry.entry_id] = runtime
    entry.runtime_data = runtime

    # Clean shutdown on HA stop (stop MQTT first)
    async def _shutdown(_event: Any) -> None:
        await runtime.async_wait_stopped()

    remove_shutdown = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    entry.async_on_unload(remove_shutdown)

    await _async_validate_login(hass, entry, client, persistence, runtime)

    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await runtime.async_stop()
        raise

    # MQTT handler
    mqtt_handler = EzvizMqttHandler(hass, client, entry.entry_id)
    runtime.push = mqtt_handler

    # Register HTTP view for image proxy/decryption once per instance
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("_http_view_registered"):
        hass.http.register_view(ImageProxyView(hass))
        domain_data["_http_view_registered"] = True

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if coordinator.shutting_down:
            raise ConfigEntryNotReady("EZVIZ setup stopped during shutdown")
    except BaseException:
        await runtime.async_stop()
        raise

    mqtt_handler.async_start()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:
    """Unload platforms; on success stop owned workers before closing HTTP."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            await runtime.async_stop()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> None:
    """Remove private credentials after any late token-producing worker exits."""
    owners = hass.data.get(DOMAIN, {}).get("_runtimes", {})
    runtime = owners.get(entry.entry_id)
    runtime = getattr(entry, "runtime_data", runtime)

    async def remove() -> None:
        if runtime is not None:
            await runtime.async_wait_stopped()
        persistence = runtime.persistence if runtime is not None else None
        await (persistence or TokenPersistence(hass, entry)).async_remove()
        if owners.get(entry.entry_id) is runtime:
            owners.pop(entry.entry_id, None)

    create_critical_task(hass, remove(), "EZVIZ credential removal")


async def async_migrate_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:  # noqa: PLR0912, PLR0915 - explicit historical migration/recovery gates
    """Consolidate historical camera settings, preserving retryability and identity."""
    if entry.version > TARGET_VERSION:
        create_migration_issue(hass, entry, "migration_future_version")
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="migration_future_version"
        )
    if entry.version == TARGET_VERSION:
        clear_migration_issue(hass, entry)
        return True

    # Old camera entries are placeholders; only their cloud account owns migration.
    if entry.data.get(CONF_TYPE) != ATTR_TYPE_CLOUD:
        return True
    if entry.version == 4:
        try:
            await TokenPersistence(hass, entry).async_promote(version=TARGET_VERSION)
        except OSError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="credential_storage_failed"
            ) from err
        clear_migration_issue(hass, entry)
        return True

    entries = hass.config_entries.async_entries(DOMAIN)
    legacy_cams = [
        candidate
        for candidate in entries
        if candidate.entry_id != entry.entry_id
        and candidate.version < 4
        and candidate.source != SOURCE_IGNORE
        and candidate.data.get(CONF_TYPE) == ATTR_TYPE_CAMERA
    ]
    # Historical flows allowed only one cloud account. Never guess ownership if
    # manually restored entries violate that invariant.
    if legacy_cams and sum(e.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD for e in entries) != 1:
        create_migration_issue(hass, entry, "migration_ambiguous_accounts")
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="migration_ambiguous_accounts"
        )
    if any(not isinstance(cam.unique_id, str) or not cam.unique_id for cam in legacy_cams):
        create_migration_issue(hass, entry, "migration_missing_serial")
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="migration_missing_serial"
        )

    options = dict(entry.options)
    cameras_map = dict(options.get(OPTIONS_KEY_CAMERAS, {}))
    for cam in legacy_cams:
        serial = cam.unique_id
        if serial in cameras_map:
            # A previous partial pass or user choice must win over old credentials.
            continue
        password = cam.data.get(CONF_PASSWORD, DEFAULT_FETCH_MY_KEY)
        enc_key = cam.data.get(CONF_ENC_KEY, DEFAULT_FETCH_MY_KEY)
        if cam.version == 1:
            # v1 -> v2 (#153) copied the RTSP password into the encryption key.
            enc_key = password
        if cam.version <= 2:
            # v2 -> v3 (#180) retained verification-code RTSP authentication.
            enc_key = enc_key or password
        cameras_map[serial] = {
            CONF_USERNAME: cam.data.get(CONF_USERNAME, DEFAULT_CAMERA_USERNAME),
            CONF_PASSWORD: password,
            CONF_ENC_KEY: enc_key,
            CONF_RTSP_USES_VERIFICATION_CODE: (
                True if cam.version <= 2 else cam.data.get(CONF_RTSP_USES_VERIFICATION_CODE, False)
            ),
            CONF_FFMPEG_ARGUMENTS: cam.options.get(CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS),
        }

    options.setdefault(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    options[OPTIONS_KEY_CAMERAS] = cameras_map
    # Save transferred settings BEFORE removal. Keep the old version until every
    # removal completes, so interruptions are retried without losing credentials.
    hass.config_entries.async_update_entry(entry, options=options)
    victims = [
        candidate
        for candidate in entries
        if candidate.entry_id != entry.entry_id
        and candidate.version < 4
        and (candidate.source == SOURCE_IGNORE or candidate in legacy_cams)
    ]
    for victim in victims:
        try:
            await hass.config_entries.async_remove(victim.entry_id)
        except (OSError, TimeoutError) as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="migration_cleanup_failed"
            ) from err
        except Exception as err:
            create_migration_issue(hass, entry, "migration_cleanup_failed")
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="migration_cleanup_failed"
            ) from err
        if hass.config_entries.async_get_entry(victim.entry_id) is not None:
            create_migration_issue(hass, entry, "migration_cleanup_failed")
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="migration_cleanup_failed"
            )

    clear_migration_issue(hass, entry)
    try:
        await TokenPersistence(hass, entry).async_promote(version=TARGET_VERSION)
    except OSError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="credential_storage_failed"
        ) from err
    _LOGGER.info("Migrated EZVIZ cloud entry %s to v%d", entry.entry_id, TARGET_VERSION)
    return True
