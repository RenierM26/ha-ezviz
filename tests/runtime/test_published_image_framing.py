"""Qualify published SDK multi-image framing through actual HA image/cache paths."""

from io import BytesIO
from struct import pack
from unittest.mock import AsyncMock, MagicMock

from Crypto.Cipher import AES
import httpx
from PIL import Image as PILImage
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
import pytest

from custom_components.ezviz_cloud import image

from .test_hardening import entities


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("trailer", [b"", b"opaque-trailer"])
async def test_published_sdk_selects_and_caches_each_plain_lens(
    hass, monkeypatch, encrypted, trailer
):
    """Declared bounds, not opaque trailers, define each encrypted image block."""
    plains = []
    for size, color in [((16, 8), (20, 60, 90)), ((12, 10), (90, 60, 20))]:
        with BytesIO() as buf, PILImage.new("RGB", size, color) as picture:
            picture.save(buf, format="JPEG")
            plains.append(buf.getvalue())
    records = []
    for plain in plains:
        block = plain
        if encrypted:
            padding = 16 - len(plain) % 16
            cipher = AES.new(b"TESTONLY" + b"\0" * 8, AES.MODE_CBC, b"01234567" + b"\0" * 8)
            block = (
                HIK_ENCRYPTION_HEADER
                + b"ac837ab144ae11d771426037bf46479e"
                + cipher.encrypt(plain + bytes([padding]) * padding)
            )
        records.append(b"meta" + pack(">II", 4 + len(block) + len(trailer), len(block)) + block + trailer)
    payload = b"mutichhikpic\0\1" + b"".join(records)
    entry, _, coordinator, _, _, second = entities(hass, lens_total=2)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "TESTONLY" if encrypted else ""}}}
    )
    first = image.EzvizLastMotion(
        hass, coordinator, "CAM001", {}, lens_index=1, lens_total=2
    )
    first.hass = hass
    first.entity_id = "image.synthetic_first"
    first.async_write_ha_state = MagicMock()
    try:
        for entity, plain in zip((first, second), plains, strict=True):
            fetch = AsyncMock(return_value=httpx.Response(200, content=payload))
            monkeypatch.setattr(entity, "_fetch_url", fetch)
            assert await entity.async_image() == plain
            assert entity._cached_image.content == plain
            assert await entity.async_image() == plain
            fetch.assert_awaited_once()
    finally:
        await coordinator.async_close()
