"""Expose Ezviz camera alarms as a media source."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, override
from urllib.parse import urlsplit

from pyezvizapi.devices.channels import alarm_camera_channel, resolve_camera_channel
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .channels import registered_lens_serials, reported_lens_channels
from .const import DOMAIN
from .views import async_generate_image_proxy_url

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_IDENTIFIER_LENGTH = 16384


def _b64e(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode()


def _b64d(data: str) -> str:
    return base64.b64decode(data.encode("ascii"), altchars=b"-_", validate=True).decode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class CameraMediaId:
    """Validated fields of an EZVIZ media identifier."""

    entry_id: str
    serial: str
    limit: int
    channel_key: str | None = None


@dataclass(frozen=True, slots=True)
class ImageMediaId:
    """Validated fields of an EZVIZ media identifier."""

    url: str
    mime: str
    entry_id: str | None = None
    serial: str | None = None
    channel_key: str | None = None


def _valid_component(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _validate_image_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ord(char) <= 32 for char in url)
    ):
        raise ValueError("Invalid image URL")
    # Validate malformed/out-of-range ports without changing signed URL contents.
    _ = parsed.port
    return url


def _validated_camera_id(identifier: str) -> CameraMediaId:
    """Validate syntax before translating boundary errors."""
    parts = identifier.split("|")
    if (
        len(identifier) <= MAX_IDENTIFIER_LENGTH
        and len(parts) == 5
        and parts[0] == "CLENS"
    ):
        _, entry_id, serial, encoded, raw_limit = parts
        if (
            not _valid_component(entry_id)
            or not _valid_component(serial)
            or not raw_limit.isascii()
            or not raw_limit.isdigit()
            or len(raw_limit) > 3
        ):
            raise ValueError
        limit = int(raw_limit)
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError
        return CameraMediaId(entry_id, serial, limit, _b64d(encoded))
    if (
        len(identifier) > MAX_IDENTIFIER_LENGTH
        or len(parts) not in (3, 4)
        or parts[0] != "CAM"
    ):
        raise ValueError
    _, entry_id, serial, *rest = parts
    if not _valid_component(entry_id) or not _valid_component(serial):
        raise ValueError
    if rest and (len(rest[0]) > 3 or not rest[0].isascii() or not rest[0].isdigit()):
        raise ValueError
    limit = int(rest[0]) if rest else DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError
    return CameraMediaId(entry_id, serial, limit)


def _parse_camera_id(identifier: str) -> CameraMediaId:
    try:
        result = _validated_camera_id(identifier)
    except (ValueError, UnicodeError, binascii.Error) as err:
        raise Unresolvable("Invalid EZVIZ camera media identifier") from err
    else:
        return result


def _validated_image_id(identifier: str) -> ImageMediaId:
    """Validate syntax before translating boundary errors."""
    parts = identifier.split("|")
    if len(identifier) > MAX_IDENTIFIER_LENGTH:
        raise ValueError
    if len(parts) in (4, 5) and parts[0] == "EIMG":
        _, entry_id, serial, encoded, *channel = parts
        if not _valid_component(entry_id) or not _valid_component(serial):
            raise ValueError
        return ImageMediaId(
            _validate_image_url(_b64d(encoded)),
            "image/jpeg",
            entry_id,
            serial,
            _b64d(channel[0]) if channel else None,
        )
    if len(parts) == 3 and parts[0] == "IMG":
        _, encoded, mime = parts
        if not re.fullmatch(r"image/[a-z0-9.+-]+", mime):
            raise ValueError
        return ImageMediaId(_validate_image_url(_b64d(encoded)), mime)
    raise ValueError


def _parse_image_id(identifier: str) -> ImageMediaId:
    try:
        result = _validated_image_id(identifier)
    except (ValueError, UnicodeError, binascii.Error) as err:
        # Never echo stored identifiers: their encoded URLs may contain credentials.
        raise Unresolvable("Invalid EZVIZ image media identifier") from err
    else:
        return result


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

    @override
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse Ezviz media items."""
        # Root: list cameras only (per-camera browsing)
        if not item.identifier:
            return await self._async_root()

        parsed = _parse_camera_id(item.identifier)
        return await self._async_camera_alarms(
            parsed.entry_id, parsed.serial, parsed.limit, channel_key=parsed.channel_key
        )

    @override
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve current proxied and legacy image identifiers safely."""
        if not item.identifier:
            raise Unresolvable("Missing media identifier")
        parsed = _parse_image_id(item.identifier)
        if parsed.entry_id is not None:
            assert parsed.serial is not None
            entry = self.hass.config_entries.async_get_entry(parsed.entry_id)
            runtime = getattr(entry, "runtime_data", None)
            if entry is None or entry.domain != DOMAIN or runtime is None:
                raise Unresolvable("EZVIZ image is unavailable; browse again")
            coordinator = runtime.coordinator
            expected = (
                (parsed.serial, parsed.url)
                if parsed.channel_key is None
                else (parsed.serial, parsed.url, parsed.channel_key)
            )
            reference = next(
                (
                    ref
                    for ref, record in coordinator.image_records.items()
                    if record == expected
                ),
                None,
            )
            if (
                reference is None
                or coordinator.shutting_down
                or parsed.serial not in coordinator.data
            ):
                raise Unresolvable("EZVIZ image is unavailable; browse again")
            if parsed.channel_key is not None:
                try:
                    resolve_camera_channel(
                        coordinator.data[parsed.serial], parsed.channel_key
                    )
                except PyEzvizError as error:
                    raise Unresolvable(
                        "EZVIZ image channel is unavailable; browse again"
                    ) from error
            return PlayMedia(
                async_generate_image_proxy_url(
                    parsed.entry_id, parsed.serial, reference
                ),
                parsed.mime,
            )
        return PlayMedia(parsed.url, parsed.mime)

    async def _async_root(self) -> BrowseMediaSource:
        children: list[BrowseMediaSource] = []

        # Enumerate loaded config entries for this domain and list cameras
        for config_entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            data = getattr(config_entry, "runtime_data", None)
            if not data:
                continue

            # Per-camera entries from coordinator data
            coordinator = data.coordinator
            registered_serials = registered_lens_serials(coordinator, config_entry)
            for serial, cam in (coordinator.data or {}).items():
                title = cam.get("name") or serial
                # Use proxied thumbnail to support decryption like camera views
                last_pic = cam.get("last_alarm_pic")
                thumb = (
                    async_generate_image_proxy_url(
                        config_entry.entry_id,
                        serial,
                        coordinator.register_image(serial, last_pic),
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
                channels = reported_lens_channels(
                    coordinator,
                    config_entry,
                    serial,
                    registered_serials=registered_serials,
                )
                if channels:
                    children.extend(
                        BrowseMediaSource(
                            domain=DOMAIN,
                            identifier=f"CLENS|{config_entry.entry_id}|{serial}|{_b64e(channel.key)}|{DEFAULT_LIMIT}",
                            media_class=MediaClass.CHANNEL,
                            media_content_type=MediaType.PLAYLIST,
                            title=f"{title} — Lens {channel.local_index}",
                            can_play=False,
                            can_expand=True,
                        )
                        for channel in channels
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
        self, entry_id: str, serial: str, limit: int, *, channel_key: str | None = None
    ) -> BrowseMediaSource:
        """Return list of recent alarms for a camera."""
        identifier = (
            f"CAM|{entry_id}|{serial}|{limit}"
            if channel_key is None
            else f"CLENS|{entry_id}|{serial}|{_b64e(channel_key)}|{limit}"
        )
        entry = self.hass.config_entries.async_get_entry(entry_id)
        data = getattr(entry, "runtime_data", None)
        if (
            entry is None
            or entry.domain != DOMAIN
            or data is None
            or data.coordinator.shutting_down
        ):
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=identifier,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.PLAYLIST,
                title=f"{serial} - recent alarms (unavailable)",
                can_play=False,
                can_expand=False,
                children=[],
            )

        coordinator = data.coordinator
        if serial not in (coordinator.data or {}):
            raise Unresolvable("Unknown EZVIZ camera")
        lens_label = ""
        if channel_key is not None:
            try:
                channel = resolve_camera_channel(coordinator.data[serial], channel_key)
                lens_label = f" — Lens {channel.local_index}"
            except PyEzvizError as error:
                raise Unresolvable("EZVIZ camera channel is unavailable") from error
        cam_name = (coordinator.data or {}).get(serial, {}).get(
            "name", serial
        ) + lens_label

        alarms = await self._async_get_alarms(coordinator, serial=serial, limit=limit)
        children: list[BrowseMediaSource] = []
        for alarm in alarms:
            item = self._alarm_to_media_item(
                entry_id, alarm, {serial: cam_name}, channel_key=channel_key
            )
            if item is not None:
                children.append(item)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            title=(
                f"{cam_name} - matches within the last {limit} device alarms"
                if channel_key is not None
                else f"{cam_name} - last {limit} alarms"
            ),
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_get_alarms(
        self, coordinator: Any, *, serial: str, limit: int
    ) -> list[dict]:
        """Fetch alarms via the client in the executor and normalize."""

        def _fetch() -> list[dict]:
            data = coordinator.ezviz_client.get_alarminfo(serial, limit)
            # Expected format: { "alarms": [ { ... } ], "page": { ... } }
            alarms = data.get("alarms") if isinstance(data, Mapping) else None
            if not isinstance(alarms, list):
                return []
            return [a for a in alarms[:limit] if isinstance(a, dict)]

        try:
            return await coordinator.async_execute(_fetch)
        except (PyEzvizError, OSError, TimeoutError, HomeAssistantError) as err:
            raise Unresolvable("EZVIZ alarms are unavailable; try again later") from err

    def _alarm_to_media_item(
        self,
        entry_id: str,
        alarm: dict,
        serial_to_name: dict[str, str] | None = None,
        *,
        channel_key: str | None = None,
    ) -> BrowseMediaSource | None:
        """Convert an alarm dictionary to an image-only BrowseMediaSource item."""
        # Build a friendly title: <time> - <camera> - <event>
        cam_serial = alarm.get("deviceSerial")
        if not isinstance(cam_serial, str) or not _valid_component(cam_serial):
            cam_serial = (
                next(iter(serial_to_name))
                if serial_to_name and len(serial_to_name) == 1
                else None
            )
        if cam_serial is None:
            return None
        if serial_to_name and cam_serial not in serial_to_name:
            return None
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
        if not isinstance(pic, str):
            return None
        try:
            _validate_image_url(pic)
        except ValueError:
            return None

        entry = self.hass.config_entries.async_get_entry(entry_id)
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None or cam_serial not in runtime.coordinator.data:
            return None
        if channel_key is not None:
            channel = alarm_camera_channel(runtime.coordinator.data[cam_serial], alarm)
            if channel is None or channel.key != channel_key:
                return None
        reference = (
            runtime.coordinator.register_image(cam_serial, pic, channel_key)
            if channel_key is not None
            else runtime.coordinator.register_image(cam_serial, pic)
        )
        # Route images through proxy for optional decryption using camera key
        img_identifier: str | None = (
            f"EIMG|{entry_id}|{cam_serial}|{_b64e(str(pic))}" if pic else None
        )
        if img_identifier is not None and channel_key is not None:
            img_identifier += f"|{_b64e(channel_key)}"
        # Build a proxied thumbnail too (avoids showing encrypted thumbs)
        proxy_serial: str = str(cam_serial or "")
        thumb = (
            async_generate_image_proxy_url(entry_id, proxy_serial, reference)
            if pic
            else None
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
