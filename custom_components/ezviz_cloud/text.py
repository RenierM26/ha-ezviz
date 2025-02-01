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
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity

SCAN_INTERVAL = timedelta(seconds=60)
PARALLEL_UPDATES = 1
_LOGGER = logging.getLogger(__name__)


TEXT_TYPE = TextEntityDescription(
    key="camera_enc_key",
    name="Camera encryption key",
    mode=TextMode.PASSWORD,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(EzvizText(coordinator, camera) for camera in coordinator.data)


class EzvizText(EzvizBaseEntity, TextEntity, RestoreEntity):
    """Representation of a EZVIZ text entity."""

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{TEXT_TYPE.key}"
        self.entity_description = TEXT_TYPE
        self._attr_native_value = None
        self.current_enc_key_hash: str | None = None
        self.mfa_enabled: bool = True

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
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self._attr_native_value:
            return False
        return super().available

    async def async_update(self) -> None:
        """Fetch data from EZVIZ."""
        _LOGGER.debug("Updating %s", self.name)

        if self.mfa_enabled and (
            not self.current_enc_key_hash
            or self.current_enc_key_hash != self.data["encrypted_pwd_hash"]
        ):
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
            self.async_write_ha_state()
