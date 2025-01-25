"""Support EZVIZ last motion image."""

from __future__ import annotations

import logging

from propcache import cached_property
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.utils import decrypt_image

from homeassistant.components.image import Image, ImageEntity, ImageEntityDescription
from homeassistant.components.text import DOMAIN as TEXT_PLATFORM
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)

IMAGE_TYPE = ImageEntityDescription(
    key="last_motion_image",
    translation_key="last_motion_image",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ image entities based on a config entry."""

    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(
        EzvizLastMotion(hass, coordinator, camera) for camera in coordinator.data
    )


class EzvizLastMotion(EzvizEntity, ImageEntity):
    """Return Last Motion Image from Ezviz Camera."""

    def __init__(
        self, hass: HomeAssistant, coordinator: EzvizDataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize a image entity."""
        EzvizEntity.__init__(self, coordinator, serial)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{serial}_{IMAGE_TYPE.key}"
        self.entity_description = IMAGE_TYPE
        self._attr_image_url = self.data["last_alarm_pic"]
        self._attr_image_last_updated = dt_util.parse_datetime(
            str(self.data["last_alarm_time"])
        )
        self.alarm_image_password: str = "Unkown"
        self.cam_key_entity_id: str | None = None

    @cached_property
    def available(self) -> bool:
        """Entity gets data from ezviz API so always available."""
        return True

    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Load an image by url."""
        if response := await self._fetch_url(url):
            image_data = response.content
            if self.alarm_image_password:
                try:
                    image_data = decrypt_image(
                        response.content, self.alarm_image_password
                    )
                except PyEzvizError:
                    _LOGGER.warning(
                        "%s: Can't decrypt last alarm picture, looks like it was encrypted with other password",
                        self.entity_id,
                    )
                    image_data = response.content
            return Image(
                content=image_data,
                content_type="image/jpeg",  # Actually returns binary/octet-stream
            )
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self.cam_key_entity_id:
            entity_registry = er.async_get(self.hass)
            self.cam_key_entity_id = entity_registry.async_get_entity_id(
                TEXT_PLATFORM, DOMAIN, f"{self._serial}_camera_enc_key"
            )

        if self.cam_key_entity_id:
            cam_enc_key_entity_state = self.hass.states.get(self.cam_key_entity_id)

            if cam_enc_key_entity_state:
                if self.alarm_image_password == "Unkown":
                    self._cached_image = None

                self.alarm_image_password = cam_enc_key_entity_state.state

        _LOGGER.debug(
            "Camera: %s, encryption entity id is: %s",
            self.entity_id,
            self.cam_key_entity_id,
        )

        _LOGGER.warning(
            "Camera: %s, encryption key is: %s",
            self.entity_id,
            self.alarm_image_password,
        )

        if (
            self.data["last_alarm_pic"]
            and self.data["last_alarm_pic"] != self._attr_image_url
        ):
            _LOGGER.debug("Image url changed to %s", self.data["last_alarm_pic"])

            self._attr_image_url = self.data["last_alarm_pic"]
            self._cached_image = None
            self._attr_image_last_updated = dt_util.parse_datetime(
                str(self.data["last_alarm_time"])
            )

        super()._handle_coordinator_update()
