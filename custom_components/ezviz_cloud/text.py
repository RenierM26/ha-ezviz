"""Support for EZVIZ text entity."""

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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
        self.mfa_enabled: bool = True
        self._entry = entry

    @property
    def current_enc_key_hash(self) -> str | None:
        """Derive from the committed key, including late/cancelled operation results."""
        key = self.coordinator.credentials.key(self._serial)
        return return_password_hash(key) if key else None

    @property
    def native_value(self) -> str:
        """A write-only credential input; even API/history must never see the key."""
        return ""

    async def async_set_value(self, value: str) -> None:
        """Set camera encryption key."""
        try:
            await self.coordinator.credentials.async_change(self._serial, value)

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set camera encryption key for {self.name}") from err


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
                await self.coordinator.credentials.async_fetch(self._serial)

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
