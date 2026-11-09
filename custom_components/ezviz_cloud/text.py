"""Support for EZVIZ text entity."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
import logging

from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    PyEzvizError,
)
from pyezvizapi.utils import return_password_hash

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENC_KEY, OPTIONS_KEY_CAMERAS
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity, EzvizEntity
from .runtime import EzvizConfigEntry

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
    hass: HomeAssistant, entry: EzvizConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    entities: list[TextEntity] = []

    for camera in coordinator.data:
        entities.append(EzvizEncryptionKeyText(coordinator, camera, entry))
        entities.append(EzvizCameraNameText(coordinator, camera))

    async_add_entities(entities)


class EzvizEncryptionKeyText(EzvizBaseEntity, TextEntity):
    """Representation of a EZVIZ text entity."""

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        entry: EzvizConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{ENC_KEY_TEXT.key}"
        self.entity_description = ENC_KEY_TEXT
        self._encryption_key: str | None = (
            (entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(serial, {}).get(CONF_ENC_KEY)
        )
        self.current_enc_key_hash: str | None = (
            return_password_hash(self._encryption_key) if self._encryption_key else None
        )
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
        self.coordinator.async_update_listeners()

    @property
    def native_value(self) -> str:
        """A write-only credential input; even API/history must never see the key."""
        return ""

    async def async_set_value(self, value: str) -> None:
        """Set camera encryption key."""
        try:
            await self.coordinator.async_execute(
                partial(
                    self.coordinator.ezviz_client.set_video_enc,
                    serial=self._serial,
                    enable=2,
                    new_password=value,
                    old_password=self._encryption_key,
                )
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set camera encryption key for {self.name}") from err

        self._encryption_key = value
        self.current_enc_key_hash = return_password_hash(value)
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
                "%s: Encryption key changed; fetching from API",
                self.entity_id,
            )

            try:
                new_encryption_key = await self.coordinator.async_execute(
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
                raise HomeAssistantError(f"Invalid response from API: {error}") from error

            self._encryption_key = new_encryption_key
            self.current_enc_key_hash = return_password_hash(new_encryption_key)
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
            await self.coordinator.async_execute(
                self.coordinator.ezviz_client.update_device_name,
                self._serial,
                value,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot rename camera {self.name}") from err

        self._attr_native_value = value
        self.async_write_ha_state()
