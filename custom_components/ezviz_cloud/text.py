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

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ENC_KEY, DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity

SCAN_INTERVAL = timedelta(seconds=3600)
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

    async_add_entities(
        EzvizText(hass, coordinator, camera) for camera in coordinator.data
    )


class EzvizText(EzvizBaseEntity, TextEntity, RestoreEntity):
    """Representation of a EZVIZ text entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{TEXT_TYPE.key}"
        self.entity_description = TEXT_TYPE
        self._attr_native_value = None
        self.camera = hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, serial
        )
        self.alarm_enc_key = (
            self.camera.data[CONF_ENC_KEY]
            if self.camera and self.camera.source != SOURCE_IGNORE
            else None
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if not (last_state := await self.async_get_last_state()):
            return self.schedule_update_ha_state(force_refresh=True)
        self._attr_native_value = last_state.state
        return None

    def set_value(self, value: str) -> None:
        """Set camera encryption key."""
        try:
            self.coordinator.ezviz_client.set_video_enc(
                serial=self._serial,
                enable=0,
                new_password=value,
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"Cannot set camera encryption key for {self.name}"
            ) from err

    async def async_update(self) -> None:
        """Fetch data from EZVIZ."""
        _LOGGER.debug("Updating %s", self.name)
        try:
            self._attr_native_value = await self.hass.async_add_executor_job(
                self.coordinator.ezviz_client.get_cam_key, self._serial
            )

        except (
            EzvizAuthTokenExpired,
            EzvizAuthVerificationCode,
            PyEzvizError,
        ) as error:
            raise HomeAssistantError(f"Invalid response from API: {error}") from error

        # Encryption key changed, update config entry
        if self.alarm_enc_key and self.camera:
            if self.alarm_enc_key != self._attr_native_value:
                await self.update_camera_config_entry(self.camera)

    async def update_camera_config_entry(self, entry: ConfigEntry) -> None:
        """Update camera config entry."""
        data = {**entry.data}
        data[CONF_ENC_KEY] = self._attr_native_value
        self.hass.config_entries.async_update_entry(
            entry,
            data=data,
        )
