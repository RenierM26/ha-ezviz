"""Expose Ezviz camera alarms as a media source."""

from __future__ import annotations

import base64
from typing import Any

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DATA_COORDINATOR, DOMAIN
from .views import async_generate_image_proxy_url

ALL_CAMERAS_ID = "ALL"
DEFAULT_LIMIT = 50


def _b64e(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode()


def _b64d(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode()


async def async_get_media_source(hass: HomeAssistant) -> EzvizMediaSource:
    """Set up Ezviz media source."""
    return EzvizMediaSource(hass)


class EzvizMediaSource(MediaSource):
    """Provide Ezviz camera alarms as a media source."""

    name = "Ezviz"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize EzvizMediaSource."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse Ezviz media items."""
        # Root: list cameras only (per-camera browsing)
        if not item.identifier:
            return await self._async_root()

        ident = item.identifier.split("|")
        if ident[0] == "CAM":
            # Camera specific alarms: CAM|<entry_id>|<serial>[|<limit>]
            _, entry_id, serial, *rest = ident
            limit = int(rest[0]) if rest else DEFAULT_LIMIT
            return await self._async_camera_alarms(entry_id, serial, limit)

        raise Unresolvable(f"Unknown media item '{item.identifier}' during browsing.")

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a media item to a URL."""
        # Supported identifiers:
        # - EIMG|<entry_id>|<serial>|<b64_url> (proxied & decrypted if needed)
        # - IMG|<b64_url>|<mime> (legacy direct)
        if not item.identifier:
            raise Unresolvable("Missing media identifier")

        if item.identifier.startswith("EIMG|"):
            _type, entry_id, serial, b64 = item.identifier.split("|", 3)
            url = _b64d(b64)
            proxy_url = async_generate_image_proxy_url(entry_id, serial, url)
            return PlayMedia(proxy_url, "image/jpeg")

        ident = item.identifier.split("|", 2)
        if ident[0] == "IMG" and len(ident) == 3:
            url = _b64d(ident[1])
            mime = ident[2]
            return PlayMedia(url, mime)

        raise Unresolvable(f"Unknown media item '{item.identifier}'.")

    async def _async_root(self) -> BrowseMediaSource:
        children: list[BrowseMediaSource] = []

        # Enumerate loaded config entries for this domain and list cameras
        for config_entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            data = self.hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
            if not data:
                continue

            # Per-camera entries from coordinator data
            coordinator = data[DATA_COORDINATOR]
            for serial, cam in (coordinator.data or {}).items():
                title = cam.get("name") or serial
                # Use proxied thumbnail to support decryption like camera views
                last_pic = cam.get("last_alarm_pic")
                thumb = (
                    async_generate_image_proxy_url(
                        config_entry.entry_id, serial, last_pic
                    )
                    if last_pic
                    else None
                )
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        # Omit limit in identifier; view handler defaults it
                        identifier=f"CAM|{config_entry.entry_id}|{serial}",
                        media_class=MediaClass.CHANNEL,
                        media_content_type=MediaType.PLAYLIST,
                        title=title,
                        thumbnail=thumb or last_pic,
                        can_play=False,
                        can_expand=True,
                    )
                )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type="",
            title="Ezviz",
            can_play=False,
            can_expand=True,
            children=children,
        )

    # Aggregated view removed per request; browsing is per camera only

    async def _async_camera_alarms(
        self, entry_id: str, serial: str, limit: int
    ) -> BrowseMediaSource:
        """Return list of recent alarms for a camera."""
        data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if not data:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"CAM|{entry_id}|{serial}|{limit}",
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.PLAYLIST,
                title=f"{serial} - recent alarms (unavailable)",
                can_play=False,
                can_expand=False,
                children=[],
            )

        coordinator = data[DATA_COORDINATOR]
        client = coordinator.ezviz_client
        cam_name = (coordinator.data or {}).get(serial, {}).get("name", serial)

        alarms = await self._async_get_alarms(client, serial=serial, limit=limit)
        children: list[BrowseMediaSource] = []
        for alarm in alarms:
            item = self._alarm_to_media_item(entry_id, alarm, {serial: cam_name})
            if item is not None:
                children.append(item)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"CAM|{entry_id}|{serial}|{limit}",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            title=f"{cam_name} - last {limit} alarms",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_get_alarms(
        self, client: Any, *, serial: str, limit: int
    ) -> list[dict]:
        """Fetch alarms via the client in the executor and normalize."""

        def _fetch() -> list[dict]:
            data = client.get_alarminfo(serial, limit)
            # Expected format: { "alarms": [ { ... } ], "page": { ... } }
            alarms = data.get("alarms") or []
            # Ensure dictionaries only
            return [a for a in alarms if isinstance(a, dict)]

        # Run sync call in executor with a bounded timeout
        return await self.hass.async_add_executor_job(_fetch)

    def _alarm_to_media_item(
        self, entry_id: str, alarm: dict, serial_to_name: dict[str, str] | None = None
    ) -> BrowseMediaSource | None:
        """Convert an alarm dictionary to an image-only BrowseMediaSource item."""
        # Build a friendly title: <time> - <camera> - <event>
        cam_serial = alarm.get("deviceSerial")
        cam_name = None
        if serial_to_name and cam_serial in serial_to_name:
            cam_name = serial_to_name.get(cam_serial)
        cam_name = cam_name or alarm.get("alarmName") or cam_serial or "Camera"

        event = alarm.get("sampleName") or alarm.get("alarmTypeName") or "Alarm"
        tstamp = alarm.get("alarmStartTimeStr") or alarm.get("alarmTimeStr")
        title = f"{cam_name} - {event}"
        if tstamp:
            title = f"{tstamp} - {title}"

        # Only use the primary picture url so users see images first
        pic = alarm.get("picUrl")
        if not pic:
            return None

        # Route images through proxy for optional decryption using camera key
        img_identifier: str | None = (
            f"EIMG|{entry_id}|{cam_serial}|{_b64e(str(pic))}" if pic else None
        )
        # Build a proxied thumbnail too (avoids showing encrypted thumbs)
        proxy_serial: str = str(cam_serial or "")
        thumb = (
            async_generate_image_proxy_url(entry_id, proxy_serial, pic) if pic else None
        )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=img_identifier,
            media_class=MediaClass.IMAGE,
            media_content_type=MediaType.IMAGE,
            title=title,
            thumbnail=thumb or pic,
            can_play=bool(img_identifier),
            can_expand=False,
        )
