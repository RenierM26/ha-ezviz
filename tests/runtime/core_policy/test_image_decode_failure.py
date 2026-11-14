"""Reject undecoded camera ciphertext and allow recovery without poisoned caches."""

from io import BytesIO
from threading import get_ident
from unittest.mock import AsyncMock

from custom_components.ezviz_cloud import image
import httpx
from PIL import Image as PILImage
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
from pyezvizapi.exceptions import PyEzvizError
import pytest

from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant

from tests.runtime.test_hardening import entities

CIPHERTEXT = b"synthetic-frame-prefix" + HIK_ENCRYPTION_HEADER + b"0" * 32 + b"x" * 64


def jpeg_bytes() -> bytes:
    """Create a synthetic image without any camera data."""
    with BytesIO() as stream, PILImage.new("RGB", (16, 8), (30, 60, 90)) as picture:
        picture.save(stream, format="JPEG")
        return stream.getvalue()


@pytest.mark.parametrize("kind", ["alarm", "snapshot"])
@pytest.mark.parametrize("failure", ["Invalid password", "Invalid image padding"])
async def test_decode_error_is_not_a_jpeg_or_cache_and_next_fetch_recovers(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    failure: str,
) -> None:
    """Preserve owned worker processing without publishing failed cipher input."""
    _, _, coordinator, _, snapshot, alarm = entities(hass)
    entity = alarm if kind == "alarm" else snapshot
    entity._attr_image_url = "https://images.example.invalid/synthetic"
    clear = jpeg_bytes()
    fetch = AsyncMock(return_value=httpx.Response(200, content=CIPHERTEXT))
    monkeypatch.setattr(entity, "_fetch_url", fetch)
    worker_threads: list[int] = []

    def process(*_args: object) -> bytes:
        worker_threads.append(get_ident())
        if len(worker_threads) == 1:
            raise PyEzvizError(failure + " PRIVATE_ERROR_MARKER")
        return clear

    monkeypatch.setattr(image, "_process_image_bytes", process)
    try:
        first = await entity.async_image()
        assert first is None
        assert entity._cached_image is None
        second = await entity.async_image()
        assert second == clear
        assert fetch.await_count == 2
        assert await entity.async_image() == clear
        assert fetch.await_count == 2
        assert worker_threads and all(
            thread != get_ident() for thread in worker_threads
        )
        assert "PRIVATE_ERROR_MARKER" not in caplog.text
        assert "configured camera key" not in caplog.text
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("kind", ["alarm", "snapshot"])
async def test_missing_key_never_passes_encrypted_bytes_through_as_jpeg(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """SDK/worker no-password passthrough must not become an encrypted HA image."""
    _, _, coordinator, _, snapshot, alarm = entities(hass)
    entity = alarm if kind == "alarm" else snapshot
    entity._attr_image_url = "https://images.example.invalid/synthetic"
    monkeypatch.setattr(
        entity,
        "_fetch_url",
        AsyncMock(return_value=httpx.Response(200, content=CIPHERTEXT)),
    )
    try:
        assert await entity.async_image() is None
        assert entity._cached_image is None
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("kind", ["alarm", "snapshot"])
async def test_stale_encrypted_cache_is_evicted_before_refetch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A poisoned cache from earlier decoding must not bypass recovery."""
    _, _, coordinator, _, snapshot, alarm = entities(hass)
    entity = alarm if kind == "alarm" else snapshot
    entity._attr_image_url = "https://images.example.invalid/synthetic"
    entity._cached_image = Image(content=CIPHERTEXT, content_type="image/jpeg")
    clear = jpeg_bytes()
    fetch = AsyncMock(return_value=httpx.Response(200, content=clear))
    monkeypatch.setattr(entity, "_fetch_url", fetch)
    try:
        assert await entity.async_image() == clear
        fetch.assert_awaited_once()
        assert entity._cached_image is not None
        assert entity._cached_image.content == clear
    finally:
        await coordinator.async_close()
