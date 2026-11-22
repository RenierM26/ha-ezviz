"""Support EZVIZ last motion image."""

import asyncio
from collections.abc import Mapping
from functools import partial
import logging
from typing import Any, override

# Supported HA image clients still use httpx, not httpx2.
import httpx  # noqa: TID251
from propcache.api import cached_property
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
from pyezvizapi.devices.channels import (
    CameraChannel,
    CameraChannelError,
    camera_channels,
    resolve_camera_channel,
)
from pyezvizapi.devices.images import (
    process_channel_images,
    process_image_bytes as _process_image_bytes,
)
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.image import (
    GET_IMAGE_TIMEOUT,
    Image,
    ImageEntity,
    ImageEntityDescription,
    infer_image_type,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util import dt as dt_util

from .channels import (
    async_add_channel_entities,
    capture_signal,
    channel_available,
    is_additional_camera_lens,
)
from .const import CONF_ENC_KEY, OPTIONS_KEY_CAMERAS
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .runtime import EzvizConfigEntry
from .workers import create_critical_task

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


def _has_residual_encryption(content: bytes) -> bool:
    """Recognize clear images before searching for an opaque vendor marker."""
    # HA's four-byte JPEG table omits legal COM-first JPEGs (FF D8 FF FE).
    return (
        not content.startswith(b"\xff\xd8\xff")
        and infer_image_type(content) is None
        and HIK_ENCRYPTION_HEADER in content
    )


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
    async_add_channel_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda serial, channel: (
            [
                EzvizChannelSnapshot(
                    hass, coordinator, serial, cams_options.get(serial, {}), channel
                )
            ]
            if is_additional_camera_lens(channel)
            else []
        ),
    )


async def async_channel_image(
    hass: HomeAssistant,
    coordinator: EzvizDataUpdateCoordinator,
    serial: str,
    channel_key: str,
    url: Any,
    password: str | None,
    *,
    known_source: bool = False,
    generation: object = None,
) -> bytes | None:
    """Share one download/decode across concurrent lens viewers; never cross lenses."""
    if coordinator.shutting_down or not isinstance(url, str) or not url:
        return None
    data = coordinator.device_data(serial)
    channels = camera_channels(data)
    try:
        channel = resolve_camera_channel(data, channel_key)
    except CameraChannelError:
        return None
    source_key = channel_key if known_source else None
    key = (serial, "channel_images", url, password, channels, source_key, generation)

    async def download() -> dict[str, bytes] | None:
        try:
            response = await get_async_client(hass).get(
                url, timeout=GET_IMAGE_TIMEOUT, follow_redirects=True
            )
            response.raise_for_status()
            return await coordinator.async_execute(
                partial(
                    process_channel_images,
                    response.content,
                    password,
                    channels,
                    source_channel=channel if known_source else None,
                )
            )
        except httpx.HTTPError, ValueError, PyEzvizError:
            return None

    try:
        result = await coordinator.async_channel_image_batch(key, download)
    except HomeAssistantError:
        if coordinator.shutting_down:
            return None
        raise
    body = result.get(channel_key) if result is not None else None
    if coordinator.shutting_down or not isinstance(body, bytes):
        return None
    if not body.startswith(b"\xff\xd8\xff") and infer_image_type(body) is None:
        return None
    return body


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
        self._image_task: asyncio.Task[Image | None] | None = None
        self._image_task_generation = -1
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
    async def _fetch_url(self, url: str) -> httpx.Response | None:
        """Fetch private cloud URLs without logging URLs or exception messages."""
        try:
            response = await self._client.get(
                url, timeout=GET_IMAGE_TIMEOUT, follow_redirects=True
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            _LOGGER.error("%s: Timeout getting camera image", self.entity_id)
            return None
        except httpx.RequestError, httpx.HTTPStatusError:
            _LOGGER.error("%s: Error getting camera image", self.entity_id)
            return None
        return response

    @override
    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Fetch asynchronously and process immutable image inputs off the loop."""
        if self.coordinator.shutting_down:
            return None
        password = self.alarm_image_password
        key = (
            self._serial,
            self.entity_description.key,
            url,
            password,
            self._image_generation,
        )
        if response := await self.coordinator.async_image_download(
            key, lambda: self._fetch_url(url)
        ):
            try:
                image_data = await self.coordinator.async_execute(
                    _process_image_bytes,
                    response.content,
                    password,
                    self._lens_index,
                    self._lens_total,
                )
            except HomeAssistantError:
                if self.coordinator.shutting_down:
                    return None
                raise
            except PyEzvizError:
                _LOGGER.warning(
                    "%s: Unable to decode camera image; image not cached",
                    self.entity_id,
                )
                return None
            if _has_residual_encryption(image_data):
                _LOGGER.warning(
                    "%s: Camera image remains encrypted; image not cached",
                    self.entity_id,
                )
                return None
            return Image(content=image_data, content_type="image/jpeg")
        return None

    @override
    async def async_image(self) -> bytes | None:
        """Reject downloads superseded by a capture, alarm URL or key update."""
        if self.coordinator.shutting_down:
            return None
        self._sync_encryption_key()
        if self._cached_image is not None:
            if not _has_residual_encryption(self._cached_image.content):
                return self._cached_image.content
            self._invalidate_image()
        url = self.image_url
        if not isinstance(url, str) or not url:
            return None
        generation = self._image_generation
        if self._image_task is None or self._image_task_generation != generation:
            # Register the full request with coordinator shutdown ownership. Shield
            # shared work so cancellation of a viewer does not cancel other viewers.
            self._image_task = create_critical_task(
                self.hass,
                self.coordinator.async_execute_owned(
                    lambda: self._async_load_image_from_url(url)
                ),
                "EZVIZ image request",
            )
            self._image_task.add_done_callback(_retrieve_image_error)
            self._image_task_generation = generation
        task = self._image_task
        try:
            image = await asyncio.shield(task)
        finally:
            if task.done() and self._image_task is task:
                self._image_task = None
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
        """Follow the cloud alarm timestamp without refetching unchanged URLs."""
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
        self._channel_key: str | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Listen for pictures captured by the camera entity."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                capture_signal(self._serial, self._channel_key),
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


class EzvizChannelSnapshot(EzvizSnapshot):
    """Only a capture addressed to this channel updates this image."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        credentials: Mapping[str, Any],
        channel: CameraChannel,
    ) -> None:
        """Preserve the existing legacy Snapshot entity and its subscription."""
        super().__init__(hass, coordinator, serial, credentials)
        self._channel_key = channel.key
        self._attr_unique_id = f"{serial}_channel_{channel.key}_snapshot"
        self.entity_description = ImageEntityDescription(
            key="channel_snapshot", translation_key="channel_snapshot"
        )
        self._attr_translation_placeholders = {"channel": channel.local_index}

    @property
    @override
    def available(self) -> bool:
        """Keep stale/missing lens identities unavailable without deleting IDs."""
        return self._channel_key is not None and channel_available(
            self.coordinator, self._serial, self._channel_key
        )

    @override
    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """A targeted capture establishes the source channel of an unindexed JPEG."""
        if self._channel_key is None or not self.available:
            return None
        body = await async_channel_image(
            self.hass,
            self.coordinator,
            self._serial,
            self._channel_key,
            url,
            self.alarm_image_password,
            known_source=True,
            generation=self._image_generation,
        )
        if body is None or not self.available:
            return None
        return Image(content=body, content_type=infer_image_type(body) or "image/jpeg")


def _retrieve_image_error(task: asyncio.Task) -> None:
    """Retrieve errors even if every shared-request waiter was cancelled."""
    if not task.cancelled():
        task.exception()
