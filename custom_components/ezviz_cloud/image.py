"""Support EZVIZ last motion image."""

from collections.abc import Mapping
import logging
from typing import Any, override

from propcache.api import cached_property
from pyezvizapi.devices.images import process_image_bytes as _process_image_bytes
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.image import Image, ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ENC_KEY, OPTIONS_KEY_CAMERAS, SIGNAL_PICTURE_CAPTURED
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .runtime import EzvizConfigEntry

_LOGGER = logging.getLogger(__name__)

IMAGE_TYPE = ImageEntityDescription(
    key="last_motion_image",
    translation_key="last_motion_image",
)
SNAPSHOT_TYPE = ImageEntityDescription(
    key="snapshot",
    translation_key="snapshot",
)

MAX_MULTI_LENS_IMAGES = 4


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ image entities based on a config entry."""

    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    cams_options: Mapping[str, dict[str, Any]] = entry.options[OPTIONS_KEY_CAMERAS]

    entities: list[EzvizLastMotion] = [
        EzvizLastMotion(
            hass,
            coordinator,
            camera,
            cams_options.get(camera, {}),
            lens_index=lens_index,
            lens_total=max(
                1,
                min(
                    int(coordinator.data[camera].get("supported_channels") or 1),
                    MAX_MULTI_LENS_IMAGES,
                ),
            ),
        )
        for camera in coordinator.data
        for lens_index in range(
            1,
            max(
                1,
                min(
                    int(coordinator.data[camera].get("supported_channels") or 1),
                    MAX_MULTI_LENS_IMAGES,
                ),
            )
            + 1,
        )
    ]
    entities.extend(
        EzvizSnapshot(hass, coordinator, camera, cams_options.get(camera, {}))
        for camera in coordinator.data
    )
    async_add_entities(entities)


class EzvizLastMotion(EzvizEntity, ImageEntity):
    """Return Last Motion Image from Ezviz Camera."""

    _cached_image: Image | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        credentials: Mapping[str, Any],
        *,
        lens_index: int = 1,
        lens_total: int = 1,
    ) -> None:
        """Initialize an image entity."""
        EzvizEntity.__init__(self, coordinator, serial)
        ImageEntity.__init__(self, hass)
        self._image_generation = 0
        self._lens_index = max(1, lens_index)
        self._lens_total = max(1, lens_total)
        unique_suffix = (
            IMAGE_TYPE.key
            if self._lens_index == 1
            else f"{IMAGE_TYPE.key}_{self._lens_index}"
        )
        self._attr_unique_id = f"{serial}_{unique_suffix}"
        self.entity_description = IMAGE_TYPE
        self._attr_image_url = self.data["last_alarm_pic"]
        self._attr_image_last_updated = dt_util.parse_datetime(
            str(self.data["last_alarm_time"])
        )
        self.alarm_image_password: str | None = credentials.get(CONF_ENC_KEY)
        if self._lens_index > 1:
            self._attr_name = f"Lens {self._lens_index} Last Motion"

    @cached_property
    @override
    def available(self) -> bool:
        """Entity gets data from EZVIZ cloud API rather than local device."""
        return True

    @override
    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Fetch asynchronously and process immutable image inputs off the loop."""
        if self.coordinator.shutting_down:
            return None
        if response := await self._fetch_url(url):
            try:
                image_data = await self.coordinator.async_execute(
                    _process_image_bytes,
                    response.content,
                    self.alarm_image_password,
                    self._lens_index,
                    self._lens_total,
                )
            except HomeAssistantError:
                if self.coordinator.shutting_down:
                    return None
                raise
            except PyEzvizError:
                _LOGGER.warning(
                    "%s: Can't decrypt image with the configured camera key",
                    self.entity_id,
                )
                image_data = response.content
            return Image(content=image_data, content_type="image/jpeg")
        return None

    @override
    async def async_image(self) -> bytes | None:
        """Reject downloads superseded by a capture, alarm URL or key update."""
        if self.coordinator.shutting_down:
            return None
        self._sync_encryption_key()
        if self._cached_image is not None:
            return self._cached_image.content
        url = self.image_url
        if not isinstance(url, str) or not url:
            return None
        generation = self._image_generation
        image = await self._async_load_image_from_url(url)
        self._sync_encryption_key()
        if (
            self.coordinator.shutting_down
            or generation != self._image_generation
            or image is None
        ):
            return None
        self._cached_image = image
        self._attr_content_type = image.content_type
        return image.content

    @callback
    def _invalidate_image(self) -> None:
        """Keep older downloads from publishing after state or key changes."""
        self._image_generation += 1
        self._cached_image = None

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        self._sync_encryption_key()

        self._sync_image_url()

        super()._handle_coordinator_update()

    @callback
    def _sync_encryption_key(self) -> None:
        """Read credentials from the config entry, never another entity's state."""
        entry = self.coordinator.config_entry
        if entry is None:
            return
        key = (
            (entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {})
            .get(self._serial, {})
            .get(CONF_ENC_KEY)
        )
        if key != self.alarm_image_password:
            self.alarm_image_password = key
            self._invalidate_image()

    def _sync_image_url(self) -> None:
        """Follow the last alarm picture reported by the cloud."""
        if self.data["last_alarm_pic"] != self._attr_image_url:
            _LOGGER.debug("Image URL changed for %s", self.entity_id)

            self._attr_image_url = self.data["last_alarm_pic"]
            self._invalidate_image()
            self._attr_image_last_updated = dt_util.parse_datetime(
                str(self.data["last_alarm_time"])
            )


class EzvizSnapshot(EzvizLastMotion):
    """Picture taken on demand with the camera's capture_picture service."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        credentials: Mapping[str, Any],
    ) -> None:
        """Initialize the snapshot image entity."""
        super().__init__(hass, coordinator, serial, credentials)
        self.entity_description = SNAPSHOT_TYPE
        self._attr_unique_id = f"{serial}_{SNAPSHOT_TYPE.key}"
        self._attr_image_url = None
        self._attr_image_last_updated = None

    @override
    async def async_added_to_hass(self) -> None:
        """Listen for pictures captured by the camera entity."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PICTURE_CAPTURED.format(self._serial),
                self._handle_picture_captured,
            )
        )

    @callback
    @override
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """The capture subscription uses serial, not the entity ID."""
        super().async_entity_id_changed(old_entity_id)

    @callback
    def _handle_picture_captured(self, pic_url: str) -> None:
        self._attr_image_url = pic_url
        self._invalidate_image()
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @override
    def _sync_image_url(self) -> None:
        """Snapshots only change through capture_picture."""
