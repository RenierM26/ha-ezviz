"""Support for EZVIZ text entity."""

from __future__ import annotations

from datetime import timedelta
import logging

from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    PyEzvizError,
)
from pyezvizapi.utils import return_password_hash

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ENC_KEY, DATA_COORDINATOR, DOMAIN, OPTIONS_KEY_CAMERAS
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity, EzvizEntity

SCAN_INTERVAL = timedelta(seconds=60)
PARALLEL_UPDATES = 1
_LOGGER = logging.getLogger(__name__)


ENC_KEY_TEXT = TextEntityDescription(
    key="camera_enc_key",
    translation_key="camera_enc_key",
    mode=TextMode.PASSWORD,
    entity_registry_enabled_default=False,
    entity_category=EntityCategory.CONFIG,
)

CAMERA_NAME_TEXT = TextEntityDescription(
    key="camera_name",
    translation_key="camera_name",
    mode=TextMode.TEXT,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities: list[TextEntity] = []

    for camera in coordinator.data:
        entities.append(EzvizEncryptionKeyText(coordinator, camera, entry))
        entities.append(EzvizCameraNameText(coordinator, camera))

    async_add_entities(entities)


class EzvizEncryptionKeyText(EzvizBaseEntity, TextEntity, RestoreEntity):
    """Representation of a EZVIZ text entity."""

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{ENC_KEY_TEXT.key}"
        self.entity_description = ENC_KEY_TEXT
        self._attr_native_value: str | None = None
        self.current_enc_key_hash: str | None = None
        self.mfa_enabled: bool = True
        self._entry = entry

    def _persist_key_to_options(self, key: str) -> None:
        """Persist the encryption key to the config entry options for this camera."""
        options = dict(self._entry.options or {})
        cameras: dict[str, dict] = dict(options.get(OPTIONS_KEY_CAMERAS, {}))
        cam_opts = dict(cameras.get(self._serial, {}))
        cam_opts[CONF_ENC_KEY] = key
        cameras[self._serial] = cam_opts
        options[OPTIONS_KEY_CAMERAS] = cameras
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if (
            last_state := await self.async_get_last_state()
        ) is None or last_state.state == STATE_UNKNOWN:
            return self.schedule_update_ha_state(force_refresh=True)
        self._attr_native_value = last_state.state
        self.current_enc_key_hash = return_password_hash(self._attr_native_value)
        return None

    def set_value(self, value: str) -> None:
        """Set camera encryption key."""
        try:
            self.coordinator.ezviz_client.set_video_enc(
                serial=self._serial,
                enable=2,
                new_password=value,
                old_password=self._attr_native_value,
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"Cannot set camera encryption key for {self.name}"
            ) from err

        self._attr_native_value = value
        self.current_enc_key_hash = return_password_hash(self._attr_native_value)
        # Store updated key in entry options for reuse by media proxy, etc.
        self._persist_key_to_options(value)

    @property
    def available(self) -> bool:
        """Entity should remain available even without a key set."""
        return super().available

    def _is_device_online(self) -> bool:
        """Return True if device status != 2 (offline)."""
        return bool(self.data.get("status") != 2)

    async def async_update(self) -> None:
        """Fetch data from EZVIZ."""
        _LOGGER.debug("Updating %s", self.name)

        if self.mfa_enabled and (
            not self.current_enc_key_hash
            or self.current_enc_key_hash != self.data["encrypted_pwd_hash"]
        ):
            # Only attempt to fetch when the device is online; otherwise wait
            if not self._is_device_online():
                _LOGGER.debug(
                    "%s: Device appears offline; postponing encryption key retrieval",
                    self.entity_id,
                )
                return

            _LOGGER.warning(
                "%s: Encryption key changed, hash_of_current = %s, hash_from_api = %s, fetching from api",
                self.entity_id,
                self.current_enc_key_hash,
                self.data["encrypted_pwd_hash"],
            )

            try:
                new_encryption_key = await self.hass.async_add_executor_job(
                    self.coordinator.ezviz_client.get_cam_key, self._serial
                )

            except EzvizAuthVerificationCode as error:
                self.mfa_enabled = False
                raise HomeAssistantError(
                    f"Update camera encryption key failed, MFA needs to be enabled: {error}"
                ) from error

            except (
                EzvizAuthTokenExpired,
                PyEzvizError,
            ) as error:
                raise HomeAssistantError(
                    f"Invalid response from API: {error}"
                ) from error

            self._attr_native_value = new_encryption_key
            self.current_enc_key_hash = return_password_hash(self._attr_native_value)
            # Store fetched key in entry options
            self._persist_key_to_options(new_encryption_key)


class EzvizCameraNameText(EzvizEntity, TextEntity):
    """Text entity allowing renaming of the EZVIZ camera."""

    _attr_mode = TextMode.TEXT

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize camera name text entity."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{CAMERA_NAME_TEXT.key}"
        self.entity_description = CAMERA_NAME_TEXT
        self._attr_native_value = self._camera_name

    async def async_set_value(self, value: str) -> None:
        """Rename the camera through the EZVIZ API."""

        try:
            await self.hass.async_add_executor_job(
                self.coordinator.ezviz_client.update_device_name,
                self._serial,
                value,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot rename camera {self.name}") from err

        self._attr_native_value = value
        self.async_write_ha_state()
