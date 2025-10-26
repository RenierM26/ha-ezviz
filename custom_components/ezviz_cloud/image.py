"""Support EZVIZ last motion image."""

from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
import logging
from typing import Any

from PIL import Image as PilImage, ImageFile, UnidentifiedImageError
from propcache.api import cached_property
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.utils import decrypt_image

from homeassistant.components.image import Image, ImageEntity, ImageEntityDescription
from homeassistant.components.text import DOMAIN as TEXT_PLATFORM
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ENC_KEY, DATA_COORDINATOR, DOMAIN, OPTIONS_KEY_CAMERAS
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)
ImageFile.LOAD_TRUNCATED_IMAGES = True

IMAGE_TYPE = ImageEntityDescription(
    key="last_motion_image",
    translation_key="last_motion_image",
)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_MULTI_LENS_IMAGES = 4


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ image entities based on a config entry."""

    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    cams_options: Mapping[str, dict[str, Any]] = entry.options[OPTIONS_KEY_CAMERAS]

    async_add_entities(
        EzvizLastMotion(
            hass,
            coordinator,
            camera,
            cams_options.get(camera, {}),
            lens_index=lens_index,
            lens_total=max(1, min(int(coordinator.data[camera].get("supported_channels") or 1), MAX_MULTI_LENS_IMAGES)),
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
    )


class EzvizLastMotion(EzvizEntity, ImageEntity):
    """Return Last Motion Image from Ezviz Camera."""

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
        self.cam_key_entity_id: str | None = None
        if self._lens_index > 1:
            self._attr_name = f"Lens {self._lens_index} Last Motion"

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        entity_registry = er.async_get(self.hass)
        self.cam_key_entity_id = entity_registry.async_get_entity_id(
            TEXT_PLATFORM, DOMAIN, f"{self._serial}_camera_enc_key"
        )

    @cached_property
    def available(self) -> bool:
        """Entity gets data from EZVIZ cloud API rather than local device."""
        return True

    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Load an image by url."""
        if response := await self._fetch_url(url):
            image_data = response.content
            if self.alarm_image_password:
                try:
                    decrypted = decrypt_image(
                        response.content, self.alarm_image_password
                    )
                    segments = _extract_jpeg_segments(decrypted) or [decrypted]
                    if len(segments) == 1 and self._lens_total > 1:
                        segments = _split_composite_image(segments[0], self._lens_total)
                    idx = min(self._lens_index - 1, len(segments) - 1)
                    image_data = segments[idx]
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

        # Fetch enc key from text entity if available.
        if (
            self.cam_key_entity_id
            and (
                cam_enc_key_entity_state := self.hass.states.get(self.cam_key_entity_id)
            )
            and cam_enc_key_entity_state.state
            not in (STATE_UNAVAILABLE, self.alarm_image_password)
        ):
            self.alarm_image_password = cam_enc_key_entity_state.state
            self._cached_image = None
            _LOGGER.debug(
                "Camera encryption key updated for %s, encryption entity id is: %s",
                self.entity_id,
                self.cam_key_entity_id,
            )

        if self.data["last_alarm_pic"] != self._attr_image_url:
            _LOGGER.debug("Image url changed to %s", self.data["last_alarm_pic"])

            self._attr_image_url = self.data["last_alarm_pic"]
            self._cached_image = None
            self._attr_image_last_updated = dt_util.parse_datetime(
                str(self.data["last_alarm_time"])
            )

        super()._handle_coordinator_update()


def _extract_jpeg_segments(image_data: bytes) -> list[bytes]:
    """Extract individual JPEG segments from a concatenated payload."""
    segments: list[bytes] = []
    cursor = image_data.find(JPEG_SOI)

    while cursor != -1:
        next_soi = image_data.find(JPEG_SOI, cursor + 2)
        eoi = image_data.find(JPEG_EOI, cursor + 2)

        if eoi != -1 and (next_soi == -1 or eoi < next_soi):
            end = eoi + 2
        elif next_soi != -1:
            end = next_soi
        elif eoi != -1:
            end = eoi + 2
        else:
            end = len(image_data)

        segment = image_data[cursor:end]
        segments.append(segment)

        if next_soi == -1:
            break
        cursor = next_soi

    return segments


def _split_composite_image(image_data: bytes, parts: int) -> list[bytes]:
    """Split a single JPEG containing multiple stacked views."""
    if parts <= 1:
        return [image_data]

    try:
        img = PilImage.open(BytesIO(image_data))
        img.load()
    except (UnidentifiedImageError, OSError) as err:
        _LOGGER.debug("Unable to decode composite multi-lens snapshot: %s", err)
        return [image_data]

    width, height = img.size
    slice_height = max(1, height // parts)
    segments: list[bytes] = []

    for idx in range(parts):
        top = idx * slice_height
        bottom = height if idx == parts - 1 else (idx + 1) * slice_height
        cropped = img.crop((0, top, width, bottom))
        with BytesIO() as buf:
            cropped.save(buf, format="JPEG", quality=90)
            segments.append(buf.getvalue())

    return segments or [image_data]
