"""Snapshot service, response and cache-generation controls using real HA."""

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from PIL import Image as PILImage
from pyezvizapi import DeviceSnapshot
from pyezvizapi.devices.snapshot import CameraState, DeviceHealth
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_cloud import camera, coordinator, image, services
from custom_components.ezviz_cloud.const import DOMAIN, SERVICE_CAPTURE_PICTURE
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.config_entries import current_entry
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform


def make(hass):
    api = SimpleNamespace(
        capture_picture=MagicMock(
            return_value={
                "captureInfo": {"picUrl": "https://images.example.invalid/new.jpg?sig=synthetic"}
            }
        )
    )
    entry = MockConfigEntry(domain=DOMAIN, options={"cameras": {}})
    entry.add_to_hass(hass)
    context = current_entry.set(entry)
    try:
        data = coordinator.EzvizDataUpdateCoordinator(
            hass, config_entry=entry, api=api, api_timeout=5
        )
    finally:
        current_entry.reset(context)
    data.data = {
        "CAM001": {
            "name": "Synthetic camera",
            "device_category": "IPC",
            "device_sub_category": "CP8",
            "version": "1",
            "status": 1,
            "local_ip": "192.0.2.1",
            "local_rtsp_port": 554,
            "last_alarm_pic": "https://images.example.invalid/alarm.jpg",
            "last_alarm_time": "2026-11-08T12:00:00+00:00",
        }
    }
    observed_at = datetime.now(UTC)
    data.inventory_snapshot = DeviceSnapshot(
        1,
        observed_at,
        {"CAM001": CameraState("CAM001", "camera", data.data["CAM001"])},
        {"CAM001": DeviceHealth(True, True, observed_at)},
    )
    hass.data[DATA_FFMPEG] = SimpleNamespace(binary="/usr/bin/ffmpeg")
    cam = camera.EzvizCamera(hass, data, "CAM001", "synthetic", "synthetic", "/stream")
    cam.hass = hass
    cam.entity_id = "camera.synthetic"
    snapshot = image.EzvizSnapshot(hass, data, "CAM001", {})
    snapshot.hass = hass
    snapshot.entity_id = "image.synthetic_snapshot"
    return api, cam, snapshot


async def test_new_snapshot_starts_empty_and_has_separate_stable_identity(hass):
    _, _, snapshot = make(hass)
    assert snapshot.unique_id == "CAM001_snapshot"
    assert snapshot.image_url is None
    assert snapshot.image_last_updated is None
    assert await snapshot.async_image() is None


async def test_capture_response_updates_snapshot_without_recording_signed_url(hass, monkeypatch):
    api, cam, snapshot = make(hass)
    monkeypatch.setattr(snapshot, "async_write_ha_state", MagicMock())
    await snapshot.async_added_to_hass()
    try:
        response = await cam.async_capture_picture()
        await hass.async_block_till_done()
        assert response["picture_url"] == snapshot.image_url
        assert snapshot.image_last_updated is not None
        assert "sig=synthetic" not in str(snapshot.state_attributes)
        api.capture_picture.assert_called_once_with("CAM001", 1)
    finally:
        await snapshot.async_remove()


async def test_repeated_capture_discards_completed_cached_picture(hass, monkeypatch):
    _, _, snapshot = make(hass)
    monkeypatch.setattr(snapshot, "async_write_ha_state", MagicMock())
    fetch = AsyncMock(
        side_effect=[httpx.Response(200, content=b"first"), httpx.Response(200, content=b"second")]
    )
    monkeypatch.setattr(snapshot, "_fetch_url", fetch)
    snapshot._handle_picture_captured("https://images.example.invalid/first")
    assert await snapshot.async_image() == b"first"
    snapshot._handle_picture_captured("https://images.example.invalid/second")
    assert await snapshot.async_image() == b"second"


async def test_alarm_poll_does_not_overwrite_captured_picture(hass, monkeypatch):
    _, _, snapshot = make(hass)
    monkeypatch.setattr(snapshot, "async_write_ha_state", MagicMock())
    snapshot._handle_picture_captured("https://images.example.invalid/capture")
    snapshot._handle_coordinator_update()
    assert snapshot.image_url.endswith("/capture")


async def test_missing_picture_is_an_action_error(hass):
    api, cam, _ = make(hass)
    api.capture_picture.return_value = {"captureInfo": {}}
    with pytest.raises(HomeAssistantError, match="no picture"):
        await cam.async_capture_picture()


@pytest.mark.parametrize("reuse_url", [False, True])
@pytest.mark.parametrize("newer_download_finishes_first", [False, True])
async def test_new_capture_is_not_poisoned_by_an_older_inflight_download(
    hass, monkeypatch, reuse_url, newer_download_finishes_first
):
    _, _, snapshot = make(hass)
    monkeypatch.setattr(snapshot, "async_write_ha_state", MagicMock())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            started.set()
            await release.wait()
            return httpx.Response(200, content=b"first")
        return httpx.Response(200, content=b"second")

    monkeypatch.setattr(snapshot, "_fetch_url", fetch)
    first_url = "https://images.example.invalid/first"
    second_url = first_url if reuse_url else "https://images.example.invalid/second"
    snapshot._handle_picture_captured(first_url)
    old = asyncio.create_task(snapshot.async_image())
    try:
        await asyncio.wait_for(started.wait(), 2)
        snapshot._handle_picture_captured(second_url)
        if newer_download_finishes_first:
            assert await snapshot.async_image() == b"second"
    finally:
        release.set()
    assert await old is None
    assert snapshot.image_url == second_url
    assert await snapshot.async_image() == b"second"
    assert len(calls) == 2  # no retry loop or extra request from the retired caller


@pytest.mark.parametrize("info", ["unexpected", ["unexpected"]])
async def test_malformed_capture_payload_is_a_controlled_action_error(hass, info):
    api, cam, _ = make(hass)
    api.capture_picture.return_value = {"captureInfo": info}
    with pytest.raises(HomeAssistantError):
        await cam.async_capture_picture()


async def test_clear_jpeg_is_unchanged_even_when_camera_has_an_encryption_key(hass, monkeypatch):
    _, _, snapshot = make(hass)
    snapshot.alarm_image_password = "SYNTHETIC"
    blob = BytesIO()
    PILImage.new("RGB", (16, 16), color=(20, 40, 60)).save(blob, format="JPEG")
    jpeg = blob.getvalue()
    monkeypatch.setattr(
        snapshot, "_fetch_url", AsyncMock(return_value=httpx.Response(200, content=jpeg))
    )
    result = await snapshot._async_load_image_from_url("https://images.example.invalid/clear")
    assert result.content == jpeg
    assert PILImage.open(BytesIO(result.content)).size == (16, 16)


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"captureInfo": None},
        {"captureInfo": {"picUrl": 1}},
        {"captureInfo": {"picUrl": ["not-a-url"]}},
        {"captureInfo": {"picUrl": "file:///tmp/private"}},
        {"captureInfo": {"picUrl": "https:///missing-host"}},
        {"captureInfo": {"picUrl": "https://[invalid"}},
        {"captureInfo": {"picUrl": "https://images.example.invalid:bad/capture"}},
        {"captureInfo": {"picUrl": "https://images.example.invalid:99999/capture"}},
    ],
)
async def test_invalid_payload_never_dispatches_a_snapshot(hass, monkeypatch, result):
    api, cam, _ = make(hass)
    api.capture_picture.return_value = result
    dispatch = MagicMock()
    monkeypatch.setattr(camera, "async_dispatcher_send", dispatch)
    with pytest.raises(HomeAssistantError, match="picture"):
        await cam.async_capture_picture()
    dispatch.assert_not_called()


async def test_fetch_failure_is_not_cached_and_does_not_retry(hass, monkeypatch):
    _, _, snapshot = make(hass)
    monkeypatch.setattr(snapshot, "async_write_ha_state", MagicMock())
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(snapshot, "_fetch_url", fetch)
    snapshot._handle_picture_captured("https://images.example.invalid/unavailable")
    assert await snapshot.async_image() is None
    assert snapshot._cached_image is None
    fetch.assert_awaited_once()


async def test_registered_capture_entity_service_returns_optional_response(hass):
    api, cam, _ = make(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry.runtime_data = SimpleNamespace(coordinator=cam.coordinator)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="camera",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    services.async_setup_services(hass)
    entities = []
    await camera.async_setup_entry(hass, entry, entities.extend)
    captured_cam = entities[0]
    captured_cam.hass = hass
    captured_cam.entity_id = "camera.synthetic"
    platform.domain_platform_entities[captured_cam.entity_id] = captured_cam
    assert (
        hass.services.supports_response(DOMAIN, SERVICE_CAPTURE_PICTURE)
        is SupportsResponse.OPTIONAL
    )
    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_CAPTURE_PICTURE,
        {"entity_id": captured_cam.entity_id},
        blocking=True,
        return_response=True,
    )
    assert result == {
        captured_cam.entity_id: {
            "picture_url": api.capture_picture.return_value["captureInfo"]["picUrl"]
        }
    }
    api.capture_picture.assert_called_once_with("CAM001", 1)
