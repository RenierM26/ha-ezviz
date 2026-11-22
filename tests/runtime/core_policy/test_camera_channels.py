"""Native HA channel targets, isolated captures, images, options and notifications."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from io import BytesIO
import json
import logging
from pathlib import Path
from struct import pack
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components.ezviz_cloud import (
    camera,
    config_flow,
    event,
    image,
    light,
    media_source as media,
    sensor,
    views,
)
from custom_components.ezviz_cloud.channels import (
    CONF_LENS_RTSP_PATHS,
    async_add_channel_entities,
)
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.image import httpx
from PIL import Image
from pyezvizapi.devices.channels import camera_channels
from pyezvizapi.devices.snapshot import CameraState, DeviceSnapshot
import pytest
import requests

from homeassistant import loader
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.media_source import Unresolvable
from homeassistant.components.stream import Stream, core as stream_core
from homeassistant.components.stream.const import (
    ATTR_SETTINGS,
    ATTR_STREAMS,
    HLS_PROVIDER,
)
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.core_policy.test_ptz_position import camera_target

PUSH_FIXTURES = Path(__file__).parents[1] / "fixtures" / "push"


@pytest.fixture(autouse=True)
def integration(hass: HomeAssistant) -> None:
    """Load actual metadata without setting up an account or any network transport."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = config_flow


def dual(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    EzvizDataUpdateCoordinator, camera.EzvizCamera, list[requests.PreparedRequest]
]:
    """Use synthetic metadata and real published/candidate SDK serializers."""
    coordinator, legacy, sent, _ = camera_target(hass, monkeypatch)
    coordinator.data["CAM001"]["supportExt"] = {"724": "1"}
    coordinator.data["CAM001"]["resourceInfos"] = [
        {"resourceIdentifier": "Video", "localIndex": 1},
        {"resourceIdentifier": "Video_1", "localIndex": "2"},
    ]
    coordinator.inventory_data = {"CAM001": dict(coordinator.data["CAM001"])}
    coordinator.config_entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    return coordinator, legacy, sent


async def add(hass: HomeAssistant, entity: Entity, domain: str) -> EntityPlatform:
    """Exercise the native platform, registry identities and translations."""
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain=domain,
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entity.coordinator.config_entry
    await platform.platform_data.async_load_translations()
    await platform.async_add_entities([entity])
    return platform


@pytest.mark.parametrize("direct", [None, " 01 "])
async def test_original_camera_auto_ptz_preserves_direct_precedence(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, direct: str | None
) -> None:
    """The original target preserves direct PTZ or uses the reported Lens 1."""
    coordinator, _, sent = dual(hass, monkeypatch)
    if direct is not None:
        coordinator.data["CAM001"]["supportExt"]["586"] = direct
    entry = coordinator.config_entry
    added: list[camera.EzvizCamera] = []
    platforms = []
    try:
        await camera.async_setup_entry(hass, entry, added.extend)
        for entity in tuple(added):
            platform = await add(hass, entity, "camera")
            platforms.append(platform)
        primary = added[0]
        await primary.async_ptz_control_coordinates(0.25, 0.75)
        resource = "Video" if direct is None else "Video_1"
        assert sent[-1].url.endswith(f"/{resource}/1/PTZManualCtrl/CtrlPTZ3DPosition")
        await primary.async_ptz_control_coordinates(0.25, 0.75, "1")
        assert sent[-1].url.endswith("/Video/1/PTZManualCtrl/CtrlPTZ3DPosition")
        coordinator.data["CAM001"]["resourceInfos"].pop(0)
        if direct is None:
            with pytest.raises(ServiceValidationError):
                await primary.async_ptz_control_coordinates(0.25, 0.75)
            assert len(sent) == 2
        else:
            await primary.async_ptz_control_coordinates(0.25, 0.75)
            assert sent[-1].url.endswith("/Video_1/1/PTZManualCtrl/CtrlPTZ3DPosition")
        await primary.async_ptz_control_coordinates(0.25, 0.75, "2")
        assert sent[-1].url.endswith("/Video_1/2/PTZManualCtrl/CtrlPTZ3DPosition")
    finally:
        for platform in platforms:
            await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("lens", ["1", "2"])
async def test_channel_target_binds_ptz_and_keeps_legacy_identity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, lens: str
) -> None:
    """A lens camera's automatic action never depends on resource ordering."""
    coordinator, legacy, sent = dual(hass, monkeypatch)
    channel = next(
        c for c in camera_channels(coordinator.data["CAM001"]) if c.local_index == lens
    )
    entity = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    platform = await add(hass, entity, "camera")
    try:
        assert legacy.unique_id == "CAM001"
        assert entity.unique_id == f"CAM001_channel_{channel.key}"
        assert entity.device_info == legacy.device_info
        assert f"Lens {lens}" in hass.states.get(entity.entity_id).name
        assert not entity.supported_features & CameraEntityFeature.STREAM
        assert await entity.async_create_stream() is None
        assert "motion_detection" not in entity.state_attributes
        coordinator.data["CAM001"]["resourceInfos"].reverse()
        await entity.async_ptz_control_coordinates(0.25, 0.75)
        assert len(sent) == 1
        assert sent[0].url.endswith(
            f"/{channel.resource_identifier}/{lens}/PTZManualCtrl/CtrlPTZ3DPosition"
        )
        with pytest.raises(ServiceValidationError):
            await entity.async_ptz_control_coordinates(
                0.25, 0.75, "2" if lens == "1" else "1"
            )
        with pytest.raises(ServiceValidationError):
            await entity.async_enable_motion_detection()
        with pytest.raises(ServiceValidationError):
            await entity.async_disable_motion_detection()
        coordinator.data["CAM001"]["resourceInfos"] = []
        assert not entity.available
        with pytest.raises(HomeAssistantError):
            await entity.async_ptz_control_coordinates(0.25, 0.75)
        assert len(sent) == 1
    finally:
        await platform.async_reset()
        await coordinator.async_close()


async def test_capture_only_updates_the_target_lens_snapshot(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual SDK capture URLs and HA subscriptions are channel-scoped."""
    coordinator, _, sent = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    cameras: list[camera.EzvizCamera] = []
    images: list[image.EzvizLastMotion] = []
    await camera.async_setup_entry(hass, entry, cameras.extend)
    await image.async_setup_entry(hass, entry, images.extend)
    primary, secondary = cameras
    snapshots = [entity for entity in images if isinstance(entity, image.EzvizSnapshot)]
    platforms = []

    def send(method: str, url: str, **_kwargs: object) -> requests.Response:
        request = requests.Request(method, url).prepare()
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        channel_number = url.split("/")[-2]
        response._content = json.dumps(
            {
                "meta": {"code": 200},
                "captureInfo": {
                    "picUrl": f"https://images.example.invalid/lens{channel_number}"
                },
            }
        ).encode()
        return response

    monkeypatch.setattr(coordinator.ezviz_client._session, "request", send)
    try:
        assert [entity.unique_id for entity in snapshots] == [
            "CAM001_snapshot",
            "CAM001_channel_Video_1:2_snapshot",
        ]
        primary_snapshot, secondary_snapshot = snapshots
        platforms.extend([await add(hass, entity, "camera") for entity in cameras])
        platforms.extend([await add(hass, entity, "image") for entity in snapshots])

        result = await primary.async_capture_picture()
        await hass.async_block_till_done()
        assert len(sent) == 1 and sent[0].url.endswith("/CAM001/1/capture")
        assert primary_snapshot.image_url == result["picture_url"]
        assert secondary_snapshot.image_url is None

        secondary_result = await secondary.async_capture_picture()
        await hass.async_block_till_done()
        assert len(sent) == 2 and sent[1].url.endswith("/CAM001/2/capture")
        assert primary_snapshot.image_url == result["picture_url"]
        assert secondary_snapshot.image_url == secondary_result["picture_url"]
        body = jpeg("blue")

        async def get(url: str, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, content=body, request=httpx.Request("GET", url))

        download = AsyncMock(side_effect=get)
        monkeypatch.setattr(
            image, "get_async_client", lambda _hass: SimpleNamespace(get=download)
        )
        assert await secondary_snapshot.async_image() == body
        rows = coordinator.data["CAM001"]["resourceInfos"]
        missing = rows.pop()
        assert not secondary_snapshot.available
        assert await secondary_snapshot.async_image() is None
        rows.append(missing)
        assert await secondary_snapshot.async_image() == body
        download.assert_awaited_once()
        for entity in snapshots:
            assert "images.example.invalid" not in json.dumps(
                hass.states.get(entity.entity_id).as_dict(), default=str
            )
        coordinator.begin_shutdown()
        with pytest.raises(HomeAssistantError):
            await secondary.async_capture_picture()
        assert len(sent) == 2
    finally:
        for platform in platforms:
            await platform.async_reset()
        await coordinator.async_close()


async def test_lens_stream_is_explicit_and_does_not_replace_primary(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither cloud IDs nor the primary RTSP path invent a second lens feed."""
    coordinator, legacy, _ = dual(hass, monkeypatch)
    channel = camera_channels(coordinator.data["CAM001"])[1]
    entry = coordinator.config_entry
    original = {
        "username": "user",
        "enc_key": "synthetic",
        "rtsp_stream_path": "/legacy",
    }
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": original}}
    )
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    assert await legacy.stream_source()
    assert not target.supported_features
    assert await target.async_create_stream() is None
    options = {**original, CONF_LENS_RTSP_PATHS: {channel.key: "/verified/lens2"}}
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": options}}
    )
    assert (await target.stream_source()).endswith("/verified/lens2")
    assert (await legacy.stream_source()).endswith("/legacy")
    assert target.supported_features & CameraEntityFeature.STREAM
    assert "synthetic" not in str(target.extra_state_attributes)
    await coordinator.async_close()


@pytest.mark.parametrize("preload", [False, True])
async def test_lens_stream_checks_health_under_lock_and_recovers_its_route(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, preload: bool
) -> None:
    """Stop the worker and cancel old HLS timers before reusing the stream."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry,
        options={
            "cameras": {"CAM001": {CONF_LENS_RTSP_PATHS: {"Video_1:2": "/second"}}}
        },
    )
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    platform = await add(hass, target, "camera")

    streams: list[Stream] = []
    hass.config.components.add("stream")
    hass.data["stream"] = {
        ATTR_SETTINGS: STREAM_SETTINGS_NON_LL_HLS,
        ATTR_STREAMS: streams,
    }
    requesting_prefs, release_prefs = asyncio.Event(), asyncio.Event()
    prefs = DynamicStreamSettings(preload_stream=preload)
    idle_handles: list[asyncio.TimerHandle] = []

    def schedule_idle(
        _hass: HomeAssistant, delay: float, action: Callable[[datetime], None]
    ) -> Callable[[], None]:
        handle = hass.loop.call_later(delay, action, datetime.now(UTC))
        idle_handles.append(handle)
        return handle.cancel

    async def get_prefs(_hass: HomeAssistant, _entity_id: str) -> DynamicStreamSettings:
        requesting_prefs.set()
        await release_prefs.wait()
        return prefs

    def run_worker(stream: Stream) -> None:
        stream._thread_quit.wait()

    camera_module = import_module(camera.Camera.__module__)
    monkeypatch.setattr(camera_module, "get_dynamic_camera_stream_settings", get_prefs)
    monkeypatch.setattr(Stream, "_run_worker", run_worker)
    monkeypatch.setattr(stream_core, "async_call_later", schedule_idle)
    try:
        async with target._stream_lifecycle_lock:
            request = asyncio.create_task(target.async_create_stream())
            await asyncio.sleep(0)
            coordinator.last_update_success = False
        assert await request is None
        assert not streams

        coordinator.last_update_success = True
        request = asyncio.create_task(target.async_create_stream())
        async with asyncio.timeout(2):
            await requesting_prefs.wait()
        coordinator.last_update_success = False
        release_prefs.set()
        assert await request is None
        assert len(streams) == 1
        assert target.stream is None

        coordinator.last_update_success = True
        original = streams[0]
        rows = coordinator.data["CAM001"]["resourceInfos"]
        for last_octet in (2, 3, 4):
            assert await target.async_create_stream() is original
            assert original.dynamic_stream_settings is prefs
            output = original.add_provider(HLS_PROVIDER)
            output.idle_timer.start()
            idle_handle = idle_handles[-1]
            assert not idle_handle.cancelled()
            await original.start()
            worker = original._thread
            assert worker is not None and worker.is_alive()

            sibling = rows.pop()
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            assert not worker.is_alive()
            assert target.stream is None
            assert await target.async_create_stream() is None

            coordinator.data["CAM001"]["local_ip"] = f"192.0.2.{last_octet}"
            rows.append(sibling)
            coordinator.async_update_listeners()
            assert await target.async_create_stream() is original
            assert original.source.endswith(f"192.0.2.{last_octet}:554/second")
            assert streams == [original]
            assert idle_handle.cancelled()
            assert HLS_PROVIDER not in original.outputs()
    finally:
        release_prefs.set()
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_lost_route_stops_core_worker_during_source_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, cancel_request: bool
) -> None:
    """A Core worker already committed to a restart cannot lose the stop signal."""
    coordinator, _, _ = dual(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={
            "cameras": {"CAM001": {CONF_LENS_RTSP_PATHS: {"Video_1:2": "/second"}}}
        },
    )
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    platform = await add(hass, target, "camera")
    stream = Stream(
        hass,
        await target.stream_source(),
        {},
        STREAM_SETTINGS_NON_LL_HLS,
        DynamicStreamSettings(preload_stream=True),
    )
    target.stream = stream
    output = stream.add_provider(HLS_PROVIDER)
    output.idle_timer.start()
    cleanup = MagicMock(wraps=output.cleanup)
    monkeypatch.setattr(output, "cleanup", cleanup)
    entered, clearing, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_clear = Event()
    cleared = asyncio.Event()
    clear = stream._thread_quit.clear
    stop = stream.stop

    def transport(
        _source: str,
        _options: object,
        _settings: object,
        _state: object,
        _converter: object,
        quit_event: Event,
    ) -> None:
        hass.loop.call_soon_threadsafe(entered.set)
        quit_event.wait()

    def clear_for_restart() -> None:
        if entered.is_set():
            hass.loop.call_soon_threadsafe(clearing.set)
            release_clear.wait()
            clear()
            hass.loop.call_soon_threadsafe(cleared.set)
            return
        clear()

    async def stop_stream() -> None:
        stopping.set()
        await stop()

    worker_module = import_module("homeassistant.components.stream.worker")
    monkeypatch.setattr(worker_module, "stream_worker", transport)
    monkeypatch.setattr(stream._thread_quit, "clear", clear_for_restart)
    monkeypatch.setattr(stream, "stop", stop_stream)
    request: asyncio.Task[Stream | None] | None = None
    recovery: asyncio.Task[Stream | None] | None = None
    try:
        await stream.start()
        worker = stream._thread
        assert worker is not None
        async with asyncio.timeout(2):
            await entered.wait()
            coordinator.data["CAM001"]["local_ip"] = "192.0.2.20"
            target._refresh_stream_source()
            await clearing.wait()
            sibling = coordinator.data["CAM001"]["resourceInfos"].pop()
            request = asyncio.create_task(target.async_create_stream())
            await stopping.wait()
            if cancel_request:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            coordinator.data["CAM001"]["resourceInfos"].append(sibling)
            recovery = asyncio.create_task(target.async_create_stream())
            await asyncio.sleep(0)
            assert not recovery.done()
            cleanup.assert_not_called()
            release_clear.set()
            assert await asyncio.shield(recovery) is stream
            if not cancel_request:
                assert await request is None
        assert not worker.is_alive()
        cleanup.assert_called_once_with()
        assert not stream.outputs()
    finally:
        release_clear.set()
        if clearing.is_set():
            await cleared.wait()
        stream._thread_quit.set()
        pending = [task for task in (request, recovery) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        await platform.async_reset()
        await stop()
        output.idle_timer.clear()
        await coordinator.async_close()


def jpeg(color: str) -> bytes:
    """Create distinguishable valid synthetic frames."""
    with BytesIO() as output, Image.new("RGB", (8, 8), color) as frame:
        frame.save(output, format="JPEG")
        return output.getvalue()


type RtspLens = tuple[EzvizDataUpdateCoordinator, camera.EzvizChannelCamera]


@pytest.fixture
async def rtsp_lens(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RtspLens]:
    """An additional lens with a configured RTSP route."""
    coordinator, _, _ = dual(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={
            "cameras": {"CAM001": {CONF_LENS_RTSP_PATHS: {"Video_1:2": "/second"}}}
        },
    )
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    target.hass = hass
    try:
        yield coordinator, target
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("change", ["missing", "unhealthy", "new_source"])
async def test_rtsp_preview_discards_frames_from_a_stale_lens(
    rtsp_lens: RtspLens, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A frame already being grabbed must not outlive its route or lens health."""
    coordinator, target = rtsp_lens
    frame = jpeg("blue")

    async def grab(*_args: object, **_kwargs: object) -> bytes:
        if change == "missing":
            coordinator.data["CAM001"]["resourceInfos"].pop()
        elif change == "unhealthy":
            coordinator.last_update_success = False
        else:
            coordinator.data["CAM001"]["local_ip"] = "192.0.2.20"
        return frame

    monkeypatch.setattr(camera.ffmpeg, "async_get_image", grab)
    assert await target.async_camera_image() is None


async def test_concurrent_rtsp_previews_share_only_matching_frame_grabs(
    rtsp_lens: RtspLens, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled viewer does not cancel a shared grab or cache a stale frame."""
    _, target = rtsp_lens
    frame = jpeg("blue")
    first_started = asyncio.Event()
    resized_started = asyncio.Event()
    release = asyncio.Event()

    async def grab(
        _hass: HomeAssistant, _source: str, *, width: int, height: int
    ) -> bytes:
        if (width, height) == (320, 240):
            first_started.set()
        else:
            resized_started.set()
        await release.wait()
        return frame

    ffmpeg_image = AsyncMock(side_effect=grab)
    monkeypatch.setattr(camera.ffmpeg, "async_get_image", ffmpeg_image)
    first = asyncio.create_task(target.async_camera_image(320, 240))
    tasks = [first]
    try:
        async with asyncio.timeout(2):
            await first_started.wait()
            second = asyncio.create_task(target.async_camera_image(320, 240))
            resized = asyncio.create_task(target.async_camera_image(640, 480))
            tasks.extend((second, resized))
            await resized_started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert ffmpeg_image.await_count == 2
        release.set()
        assert await asyncio.gather(second, resized) == [frame, frame]
        assert await target.async_camera_image(320, 240) == frame
        assert ffmpeg_image.await_count == 3
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_slow_rtsp_preview_falls_back_without_blocking_cloud_shutdown(
    hass: HomeAssistant, rtsp_lens: RtspLens, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal HA image request falls back while the shared grab is pending."""
    coordinator, target = rtsp_lens
    platform = await add(hass, target, "camera")
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    still = jpeg("blue")
    coordinator.merge_mqtt_update(
        "CAM001",
        {
            "ext": {
                "channel_type": 1,
                "channel_no": 2,
                "image": "https://example.invalid/still",
            }
        },
    )

    async def grab(*_args: object, **_kwargs: object) -> bytes:
        started.set()
        try:
            await release.wait()
            return jpeg("red")
        finally:
            finished.set()

    ffmpeg_image = AsyncMock(side_effect=grab)
    monkeypatch.setattr(camera.ffmpeg, "async_get_image", ffmpeg_image)
    monkeypatch.setattr(camera, "RTSP_PREVIEW_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(camera, "async_channel_image", AsyncMock(return_value=still))
    camera_component = import_module("homeassistant.components.camera")
    try:
        async with asyncio.timeout(2):
            preview = await camera_component._async_get_image(target, 1, 320, 240)
            assert preview.content == still
            assert started.is_set() and not finished.is_set()
            await platform.async_reset()
            await coordinator.async_close()
        assert not finished.is_set()
        ffmpeg_image.assert_awaited_once()
    finally:
        release.set()
        if started.is_set():
            await finished.wait()
        await platform.async_reset()


async def test_malformed_cloud_url_falls_through_to_alternate_lens_still(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid preferred URL cannot block the next verified still."""
    coordinator, _, _ = dual(hass, monkeypatch)
    valid_url = "https://images.example.invalid/poll"
    malformed_url = "https://images.example.invalid:bad/push"
    coordinator.inventory_data["CAM001"]["last_alarm_pic"] = valid_url
    coordinator.inventory_data["CAM001"].pop("last_alarm_time", None)
    coordinator.merge_mqtt_update(
        "CAM001", {"ext": {"channel_type": 1, "channel_no": 2, "image": malformed_url}}
    )
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    body = jpeg("blue")
    payload = (
        b"mutichhikpic\x81\xfe"
        + pack(">HHII", 2, 0xCAFE, 4 + len(body), len(body))
        + body
    )

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        assert url == valid_url
        return httpx.Response(200, content=payload, request=request)

    mock_get = AsyncMock(side_effect=get)
    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=mock_get)
    )
    try:
        assert await target.async_camera_image() == body
        assert [call.args[0] for call in mock_get.await_args_list] == [
            malformed_url,
            valid_url,
        ]
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("multi_lens", [False, True])
async def test_stills_only_shows_alarm_images_on_original_camera(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, multi_lens: bool
) -> None:
    """The original camera serves only verified stills without starting RTSP."""
    coordinator, _, sent = dual(hass, monkeypatch)
    if not multi_lens:
        coordinator.data["CAM001"]["resourceInfos"].pop()
    coordinator.inventory_data["CAM001"].pop("last_alarm_pic", None)
    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"rtsp_stream_path": None}}}
    )
    ffmpeg_image = AsyncMock()
    monkeypatch.setattr(camera.ffmpeg, "async_get_image", ffmpeg_image)
    first, second = jpeg("red"), jpeg("blue")

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        body = first if request.url.path == "/first" else second
        return httpx.Response(200, content=body, request=request)

    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )

    def push(channel: int, path: str | None = None) -> None:
        ext = {"channel_type": 1, "channel_no": channel, "msgId": f"{channel}-{path}"}
        if path is not None:
            ext["image"] = f"https://images.example.invalid/{path}"
        coordinator.merge_mqtt_update("CAM001", {"ext": ext})

    added: list[camera.EzvizCamera] = []
    try:
        await camera.async_setup_entry(hass, entry, added.extend)
        primary = added[0]
        assert primary.unique_id == "CAM001"
        assert not primary.supported_features
        assert not await primary.stream_source()
        assert await primary.async_create_stream() is None
        assert await primary.async_camera_image() is None

        if multi_lens:
            push(2, "second")
            assert await primary.async_camera_image() is None
            assert await added[1].async_camera_image() == second

        push(1, "first")
        assert await primary.async_camera_image() == first
        push(1)
        assert await primary.async_camera_image() == first
        ffmpeg_image.assert_not_awaited()
        assert not sent  # Loading the preview never calls Capture picture.
    finally:
        await coordinator.async_close()


async def test_concurrent_lens_images_share_download_and_keep_identity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch SDK decoding selects channel IDs, not request/record order."""
    coordinator, _, _ = dual(hass, monkeypatch)
    channels = camera_channels(coordinator.data["CAM001"])
    first, second = jpeg("red"), jpeg("blue")
    payload = b"mutichhikpic\x81\xfe" + b"".join(
        pack(">HHII", number, 0xCAFE, 4 + len(body), len(body)) + body
        for number, body in [(2, second), (1, first)]
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        started.set()
        await release.wait()
        return httpx.Response(200, content=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )
    targets = [
        camera.EzvizChannelCamera(hass, coordinator, "CAM001", c) for c in channels
    ]
    tasks = [asyncio.create_task(t.async_camera_image()) for t in targets]
    try:
        await started.wait()
        release.set()
        assert await asyncio.gather(*tasks) == [first, second]
        assert await targets[0].async_camera_image() == first
        assert await targets[1].async_camera_image() == second
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.async_close()


@pytest.mark.parametrize("response_kind", ["html", "truncated_jpeg", "ambiguous"])
async def test_invalid_lens_batch_retries_the_same_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, response_kind: str
) -> None:
    """An HTTP 200 alone does not make a response a reusable lens batch."""
    coordinator, _, _ = dual(hass, monkeypatch)
    body = jpeg("blue")
    if response_kind == "ambiguous":
        invalid = body
        valid = (
            b"mutichhikpic\x81\xfe"
            + pack(">HHII", 2, 0xCAFE, 4 + len(body), len(body))
            + body
        )
    else:
        invalid = (
            b"<html>Temporary error</html>" if response_kind == "html" else body[:40]
        )
        valid = body
    url = "https://images.example.invalid/retry"
    get = AsyncMock(
        side_effect=[
            httpx.Response(200, content=content, request=httpx.Request("GET", url))
            for content in (invalid, valid)
        ]
    )
    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )
    try:
        for expected in (None, body, body):
            assert (
                await image.async_channel_image(
                    hass,
                    coordinator,
                    "CAM001",
                    "Video_1:2",
                    url,
                    None,
                    known_source=response_kind != "ambiguous",
                    generation=1,
                )
                == expected
            )
        assert get.await_count == 2
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("push_time", "poll_hour", "polled_channel", "expected"),
    [
        ("2026-11-08 12:00:00", 13, 2, "poll"),
        ("2026-11-08 14:00:00", 13, 2, "push"),
        ("2026-11-08 12:00:00", 13, 1, "push"),
        (None, 13, 2, "poll"),
        (None, 11, 2, "push"),
        ("2026-11-08T10:00:00Z", 13, 2, "poll"),
        (1794132000000, 13, 2, "poll"),
    ],
)
async def test_lens_image_orders_poll_and_push(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    push_time: str | int | None,
    poll_hour: int,
    polled_channel: int,
    expected: str,
) -> None:
    """Newer same-lens stills win, but other lenses cannot displace a push."""
    coordinator, _, _ = dual(hass, monkeypatch)
    images = {"/old": jpeg("red"), "/push": jpeg("blue"), "/poll": jpeg("green")}
    old = {
        **coordinator.inventory_data["CAM001"],
        "cam_timezone": "Africa/Johannesburg",
        "last_alarm_pic": "https://images.example.invalid/old",
        "last_alarm_time": "2026-11-08 11:00:00",
    }
    polled = old

    def poll() -> DeviceSnapshot:
        snapshot = coordinator.inventory_snapshot
        assert snapshot is not None
        return replace(
            snapshot,
            devices={"CAM001": CameraState("CAM001", "camera", polled)},
        )

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        path = request.url.path
        body = images[path]
        if path != "/push":
            channel = 2 if path == "/old" else polled_channel
            body = (
                b"mutichhikpic\x81\xfe"
                + pack(">HHII", channel, 0xCAFE, 4 + len(body), len(body))
                + body
            )
        return httpx.Response(200, content=body, request=request)

    monkeypatch.setattr(coordinator, "_poll_sync", poll)
    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )

    def push(channel: int, url: str) -> None:
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "channel_type": 1,
                    "channel_no": channel,
                    "image": url,
                    "time": push_time,
                }
            },
        )

    try:
        await coordinator.async_refresh()
        channel = camera_channels(coordinator.data["CAM001"])[1]
        target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
        assert await target.async_camera_image() == images["/old"]
        push(2, "https://images.example.invalid/push")
        assert await target.async_camera_image() == images["/push"]
        polled = {
            **old,
            "last_alarm_pic": "https://images.example.invalid/poll",
            "last_alarm_time": f"2026-11-08 {poll_hour}:00:00",
        }
        await coordinator.async_refresh()
        # An unrelated push updates the legacy device-wide image, not the poll.
        push(1, "https://images.example.invalid/sibling")
        assert await target.async_camera_image() == images[f"/{expected}"]
        if push_time is not None:
            # The same comparison also applies to a notification arriving late.
            push(2, "https://images.example.invalid/push")
            assert await target.async_camera_image() == images[f"/{expected}"]
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("with_image", [True, False])
@pytest.mark.parametrize(
    ("fixture_name", "lens_count", "target_channel", "code"),
    [
        ("single_lens_motion", 1, "Video:1", 10002),
        ("multi_lens_human", 2, "Video_1:2", 10120),
    ],
)
async def test_sanitized_push_routes_to_the_explicit_lens(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    lens_count: int,
    target_channel: str,
    code: int,
    with_image: bool,
) -> None:
    """Replay production-shaped pushes without needing any network media."""
    fixture_path = PUSH_FIXTURES / f"{fixture_name}.json"
    envelope = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert envelope["event_type"] == "ezviz_push_event"
    message = envelope["data"]
    if not with_image:
        del message["ext"]["image"]

    coordinator, _, _ = dual(hass, monkeypatch)
    channels = coordinator.data["CAM001"]["resourceInfos"][:lens_count]
    coordinator.data["CAM001"]["resourceInfos"] = channels
    entry = coordinator.config_entry
    registry = er.async_get(hass)
    legacy = (
        registry.async_get_or_create(
            "event",
            DOMAIN,
            f"CAM001_channel_{target_channel}_notification",
            config_entry=entry,
            suggested_object_id="camera_lens_2_notification",
        )
        if lens_count > 1
        else None
    )
    discovered: list[event.EzvizChannelNotification] = []
    platforms = []
    try:
        await event.async_setup_entry(hass, entry, discovered.extend)
        assert len(discovered) == lens_count
        entities = {entity.unique_id: entity for entity in discovered}
        target_entity = entities[f"CAM001_channel_{target_channel}_notification"]
        platforms = [await add(hass, entity, "event") for entity in discovered]
        if legacy is not None:
            assert target_entity.entity_id == legacy.entity_id
        coordinator.merge_mqtt_update("CAM001", message)
        await hass.async_block_till_done()
        target = hass.states.get(target_entity.entity_id)
        assert target is not None and target.state != "unknown"
        assert target.name.endswith(f"Lens {lens_count} last notification")
        assert target.attributes["event_type"] == "notification"
        assert target.attributes["code"] == code
        assert target.attributes["source_time"] == "2025-01-01 00:00:00"
        assert "images.example.invalid" not in str(target.attributes)
        assert (("CAM001", target_channel) in coordinator.channel_images) == with_image
        for entity in discovered:
            if entity is not target_entity:
                sibling = hass.states.get(entity.entity_id)
                assert sibling is not None and sibling.state == "unknown"

        coordinator.merge_mqtt_update("CAM001", message)
        unscoped = {**message["ext"], "msgId": "device-wide-notification"}
        del unscoped["channel_no"]
        coordinator.merge_mqtt_update("CAM001", {**message, "ext": unscoped})
        await hass.async_block_till_done()
        assert hass.states.get(target.entity_id) == target
    finally:
        for platform in platforms:
            await platform.async_reset()
        await coordinator.async_close()


async def test_notifications_are_channel_scoped_deduplicated_and_private(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown/no-ID messages never invent a lens event or expose signed URLs."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entities = [
        event.EzvizChannelNotification(coordinator, "CAM001", c)
        for c in camera_channels(coordinator.data["CAM001"])
    ]
    platforms = [await add(hass, e, "event") for e in entities]
    received = [MagicMock(), MagicMock()]
    for entity, callback in zip(entities, received, strict=True):
        monkeypatch.setattr(entity, "_trigger_event", callback)
    message = {
        "ext": {
            "channel_type": 1,
            "channel_no": 2,
            "msgId": "synthetic-event",
            "image": "https://images.example.invalid/signed",
            "alert_type_code": 1,
        }
    }
    try:
        coordinator.merge_mqtt_update("CAM001", message)
        coordinator.merge_mqtt_update("CAM001", message)
        await hass.async_block_till_done()
        received[0].assert_not_called()
        received[1].assert_called_once_with(
            "notification", {"code": 1, "source_time": None}
        )
        assert "signed" not in str(received[1].call_args)
        assert ("CAM001", "Video_1:2") in coordinator.channel_images
        er.async_get(hass).async_update_entity(
            entities[1].entity_id, new_entity_id="event.renamed_channel"
        )
        await hass.async_block_till_done()
        coordinator.merge_mqtt_update(
            "CAM001", {"ext": {**message["ext"], "msgId": "after-rename"}}
        )
        await hass.async_block_till_done()
        assert received[1].call_count == 2
        for index in (None, 9, True):
            coordinator.merge_mqtt_update(
                "CAM001",
                {"ext": {**message["ext"], "channel_no": index, "msgId": str(index)}},
            )
        await hass.async_block_till_done()
        assert received[1].call_count == 2
        coordinator.begin_shutdown()
        coordinator.merge_mqtt_update(
            "CAM001", {"ext": {**message["ext"], "msgId": "late"}}
        )
        await hass.async_block_till_done()
        assert received[1].call_count == 2
    finally:
        for platform in platforms:
            await platform.async_reset()
        await coordinator.async_close()


async def test_delayed_push_delivers_event_without_replacing_newer_lens_image(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare event time, not receive order, without dropping notifications."""
    coordinator, _, _ = dual(hass, monkeypatch)
    coordinator.inventory_data["CAM001"].pop("last_alarm_pic", None)
    channel = camera_channels(coordinator.data["CAM001"])[1]
    notification = event.EzvizChannelNotification(coordinator, "CAM001", channel)
    platform = await add(hass, notification, "event")
    received = MagicMock()
    monkeypatch.setattr(notification, "_trigger_event", received)
    lens = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    images = {"/first": jpeg("blue"), "/delayed": jpeg("red"), "/later": jpeg("green")}

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=images[request.url.path], request=request)

    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )

    def push(path: str, stamp: str | int) -> None:
        coordinator.merge_mqtt_update(
            "CAM001",
            {
                "ext": {
                    "channel_type": 1,
                    "channel_no": 2,
                    "msgId": path,
                    "image": f"https://images.example.invalid{path}",
                    "time": stamp,
                    "alert_type_code": 1,
                }
            },
        )

    try:
        push("/first", "2026-11-08T12:00:00Z")
        assert await lens.async_camera_image() == images["/first"]
        push("/delayed", 1794135600000)  # 11:00 UTC
        await hass.async_block_till_done()
        assert received.call_count == 2
        assert await lens.async_camera_image() == images["/first"]
        push("/later", "2026-11-08T15:00:00+02:00")
        assert await lens.async_camera_image() == images["/later"]
    finally:
        await platform.async_reset()
        await coordinator.async_close()


async def test_discovery_keeps_stable_ids_and_does_not_duplicate_shared_entities(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New snapshot discovery adds channels once, even after a missing channel."""
    coordinator, _, _ = dual(hass, monkeypatch)
    added = []
    async_add_channel_entities(
        coordinator,
        coordinator.config_entry,
        added.extend,
        lambda serial, channel: [
            camera.EzvizChannelCamera(hass, coordinator, serial, channel)
        ],
    )
    try:
        assert len(added) == 2
        original = coordinator.data["CAM001"]["resourceInfos"]
        coordinator.data["CAM001"]["resourceInfos"] = original[:1]
        coordinator.inventory_snapshot = replace(
            coordinator.inventory_snapshot, revision=2
        )
        coordinator.async_update_listeners()
        assert len(added) == 2 and not added[1].available
        coordinator.data["CAM001"]["resourceInfos"] = list(reversed(original))
        coordinator.inventory_snapshot = replace(
            coordinator.inventory_snapshot, revision=3
        )
        coordinator.async_update_listeners()
        assert len(added) == 2 and added[1].available
    finally:
        await coordinator.async_close()


async def test_channel_capture_shutdown_drains_worker_without_late_snapshot(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted device command cannot publish after its target starts unloading."""
    coordinator, _, _ = dual(hass, monkeypatch)
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    platform = await add(hass, target, "camera")
    started, release = Event(), Event()
    closed = MagicMock()
    dispatched = MagicMock()

    def capture(_serial: str, number: int) -> dict[str, object]:
        assert number == 2
        started.set()
        assert release.wait(5)
        return {"captureInfo": {"picUrl": "https://images.example.invalid/late"}}

    monkeypatch.setattr(coordinator.ezviz_client, "capture_picture", capture)
    monkeypatch.setattr(coordinator.ezviz_client, "close_session", closed)
    monkeypatch.setattr(camera, "async_dispatcher_send", dispatched)
    task = asyncio.create_task(target.async_capture_picture())
    try:
        assert await hass.async_add_executor_job(started.wait, 5)
        stopping = asyncio.create_task(coordinator.async_close())
        await asyncio.sleep(0)
        assert not closed.called
        release.set()
        with pytest.raises(HomeAssistantError):
            await task
        await stopping
        closed.assert_called_once()
        dispatched.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("change", ["key", "poll"])
async def test_lens_image_does_not_publish_after_source_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """An in-flight image belongs to its original key and channel generation."""
    coordinator, _, _ = dual(hass, monkeypatch)
    channel = camera_channels(coordinator.data["CAM001"])[1]
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
    started, release = asyncio.Event(), asyncio.Event()

    async def get(url: str, **_kwargs: object) -> httpx.Response:
        started.set()
        await release.wait()
        body = jpeg("blue")
        payload = (
            b"mutichhikpic\x81\xfe"
            + pack(">HHII", 2, 0xCAFE, 4 + len(body), len(body))
            + body
        )
        return httpx.Response(200, content=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(
        image, "get_async_client", lambda _hass: SimpleNamespace(get=get)
    )
    task = asyncio.create_task(target.async_camera_image())
    try:
        await started.wait()
        if change == "key":
            hass.config_entries.async_update_entry(
                coordinator.config_entry,
                options={"cameras": {"CAM001": {"enc_key": "new-key"}}},
            )
        else:
            coordinator.inventory_data["CAM001"]["last_alarm_time"] = (
                "2026-11-08 13:00:00"
            )
        release.set()
        assert await task is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await coordinator.async_close()


async def test_lens_options_conflict_preserves_concurrent_path_changes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent options writes require review instead of overwriting another lens."""
    coordinator, _, _ = dual(hass, monkeypatch)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    entry = coordinator.config_entry
    manager = hass.config_entries.options
    try:
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "camera_select"}
        )
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM001"})
        assert result["step_id"] == "camera_menu"
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "lens_streams"}
        )
        result = await manager.async_configure(
            result["flow_id"], {"lens_1": "substream", "lens_2": "custom"}
        )
        hass.config_entries.async_update_entry(
            entry,
            options={
                "cameras": {
                    "CAM001": {CONF_LENS_RTSP_PATHS: {"Video:1": "/concurrent"}}
                }
            },
        )
        result = await manager.async_configure(result["flow_id"], {"lens_2": "/second"})
        assert result["errors"] == {"base": "options_changed"}
        assert entry.options["cameras"]["CAM001"][CONF_LENS_RTSP_PATHS] == {
            "Video:1": "/concurrent"
        }
        result = await manager.async_configure(
            result["flow_id"], {"lens_1": "substream", "lens_2": "custom"}
        )
        result = await manager.async_configure(result["flow_id"], {"lens_2": "/second"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"][CONF_LENS_RTSP_PATHS] == {
            "Video:1": "/concurrent",
            "Video_1:2": "/second",
        }
    finally:
        await coordinator.async_close()


async def test_lens_options_keep_primary_when_no_reported_channels_remain(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel count alone does not invent a lens target."""
    coordinator, _, _ = dual(hass, monkeypatch)
    manager = hass.config_entries.options
    try:
        result = await manager.async_init(coordinator.config_entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "camera_select"}
        )
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM001"})
        coordinator.data["CAM001"]["resourceInfos"] = []
        coordinator.data["CAM001"]["supported_channels"] = 2
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "lens_streams"}
        )
        assert result["data_schema"]({}) == {"lens_1": "substream"}
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("known_camera_lens", [True, False])
async def test_only_multi_lens_registry_evidence_restores_a_remaining_camera_lens(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, known_camera_lens: bool
) -> None:
    """A saved notification alone does not establish that a camera has extra lenses."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "event", DOMAIN, "CAM001_channel_Video_1:2_notification", config_entry=entry
    )
    if known_camera_lens:
        registry.async_get_or_create(
            "camera", DOMAIN, "CAM001_channel_Video_1:2", config_entry=entry
        )
    coordinator.data["CAM001"]["resourceInfos"] = coordinator.data["CAM001"][
        "resourceInfos"
    ][1:]
    added = []
    try:
        async_add_channel_entities(
            coordinator,
            entry,
            added.extend,
            lambda serial, channel: [
                camera.EzvizChannelCamera(hass, coordinator, serial, channel)
            ],
        )
        assert len(added) == int(known_camera_lens)
        if known_camera_lens:
            assert added[0].unique_id == "CAM001_channel_Video_1:2"
            assert added[0].available
    finally:
        await coordinator.async_close()


async def test_media_root_scans_lens_registry_once_per_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the camera with registry evidence gets a surviving lens folder."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    devices = coordinator.data
    devices["CAM001"]["resourceInfos"].pop(0)
    devices["CAM002"] = {**devices["CAM001"], "last_alarm_pic": None}
    er.async_get(hass).async_get_or_create(
        "camera", DOMAIN, "CAM001_channel_Video_1:2", config_entry=entry
    )
    monkeypatch.setattr(
        hass.config_entries, "async_loaded_entries", lambda _domain: [entry]
    )
    scan = MagicMock(wraps=er.async_entries_for_config_entry)
    monkeypatch.setattr(er, "async_entries_for_config_entry", scan)
    try:
        result = await media.EzvizMediaSource(hass).async_browse_media(
            SimpleNamespace(identifier=None)
        )
        lens_folders = [
            item.identifier
            for item in result.children
            if item.identifier.startswith("CLENS|")
        ]
        assert lens_folders == [
            f"CLENS|{entry.entry_id}|CAM001|{media._b64e('Video_1:2')}|50"
        ]
        scan.assert_called_once_with(er.async_get(hass), entry.entry_id)
    finally:
        await coordinator.async_close()


async def test_channel_media_filters_history_and_binds_the_resolved_lens(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unscoped history remains in the legacy folder; forged channel scopes fail."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    url = "https://images.example.invalid/history"
    alarms = [
        {"deviceSerial": "CAM001", "channel": number, "picUrl": url}
        for number in (1, 2, None)
    ]
    monkeypatch.setattr(
        coordinator.ezviz_client,
        "get_alarminfo",
        MagicMock(return_value={"alarms": alarms}),
    )
    source = media.EzvizMediaSource(hass)
    identifier = f"CLENS|{entry.entry_id}|CAM001|{media._b64e('Video_1:2')}|50"
    try:
        result = await source.async_browse_media(SimpleNamespace(identifier=identifier))
        assert result.identifier == identifier and len(result.children) == 1
        item = result.children[0]
        assert "Lens 2" in item.title
        resolved = await source.async_resolve_media(
            SimpleNamespace(identifier=item.identifier)
        )
        assert resolved.url.startswith("/api/ezviz_cloud/image/")
        coordinator.register_image("CAM001", url)
        forged = item.identifier.rsplit("|", 1)[0] + "|" + media._b64e("Video:1")
        with pytest.raises(Unresolvable):
            await source.async_resolve_media(SimpleNamespace(identifier=forged))
        coordinator.data["CAM001"]["resourceInfos"] = []
        with pytest.raises(Unresolvable):
            await source.async_resolve_media(
                SimpleNamespace(identifier=item.identifier)
            )
    finally:
        await coordinator.async_close()


async def test_channel_proxy_uses_sdk_selection_and_keeps_authorization_private(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opaque references bind image channel selection without bypassing URL checks."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    first, second = jpeg("red"), jpeg("blue")
    payload = b"mutichhikpic\x81\xfe" + b"".join(
        pack(">HHII", number, 0xCAFE, 4 + len(body), len(body)) + body
        for number, body in [(1, first), (2, second)]
    )
    response = SimpleNamespace(
        status=200,
        content_type="image/jpeg",
        read=AsyncMock(return_value=payload),
        release=MagicMock(),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(views, "ClientSession", lambda **_kwargs: session)
    proxy = views.ImageProxyView(hass)
    try:
        reference = coordinator.register_image(
            "CAM001", "https://images.example.invalid/channel", "Video_1:2"
        )
        result = await proxy.get(
            SimpleNamespace(headers={"Authorization": "PRIVATE"}),
            entry.entry_id,
            "CAM001",
            reference,
        )
        assert result.status == 200 and result.body == second
        assert "Authorization" not in session.get.call_args.kwargs["headers"]
        private = coordinator.register_image(
            "CAM001", "http://127.0.0.1/private", "Video_1:2"
        )
        assert (
            await proxy.get(SimpleNamespace(), entry.entry_id, "CAM001", private)
        ).status == 400
        coordinator.data["CAM001"]["resourceInfos"] = []
        assert (
            await proxy.get(SimpleNamespace(), entry.entry_id, "CAM001", reference)
        ).status == 404
        assert session.get.await_count == 1
        coordinator.data["CAM001"]["resourceInfos"] = [
            {"resourceIdentifier": "Video_1", "localIndex": "2"}
        ]

        async def removed_during_fetch() -> bytes:
            coordinator.data.pop("CAM001")
            return payload

        response.read.side_effect = removed_during_fetch
        result = await proxy.get(SimpleNamespace(), entry.entry_id, "CAM001", reference)
        assert result.status == 400 and result.body != second
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("path", ["/verified/lens2", ""])
async def test_lens_options_use_native_reload_and_preserve_other_camera_options(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The form stores only a path and does not fetch credentials or probe RTSP."""
    coordinator, _, _ = dual(hass, monkeypatch)
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry,
        options={
            "timeout": 41,
            "cameras": {
                "CAM001": {
                    "enc_key": "keep",
                    "rtsp_stream_path": "/legacy",
                    "unknown": 7,
                    CONF_LENS_RTSP_PATHS: {"Video:1": "/lens1", "Video_1:2": "/old"},
                }
            },
        },
    )
    manager = hass.config_entries.options
    try:
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "camera_select"}
        )
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM001"})
        assert result["step_id"] == "camera_menu"
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "lens_streams"}
        )
        result = await manager.async_configure(
            result["flow_id"],
            {"lens_1": "custom", "lens_2": "custom" if path else "stills"},
        )
        assert result["step_id"] == "lens_stream_edit"
        assert result["data_schema"]({})["lens_1"] == "/legacy"
        if path:
            result = await manager.async_configure(
                result["flow_id"], {"lens_2": "rtsp://private.invalid/credentials"}
            )
            assert result["errors"] == {"lens_2": "invalid_rtsp_path"}
        result = await manager.async_configure(
            result["flow_id"], {"lens_2": path} if path else {}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        options = entry.options["cameras"]["CAM001"]
        assert options["enc_key"] == "keep" and options["unknown"] == 7
        assert options["rtsp_stream_path"] == "/legacy"
        assert options[CONF_LENS_RTSP_PATHS].get("Video_1:2", "") == path
        assert options[CONF_LENS_RTSP_PATHS]["Video:1"] == "/lens1"
        reload_entry.assert_awaited_once_with(entry.entry_id)
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("mode", "suffix"),
    [("main_stream", "101"), ("substream", "102"), ("custom", "103"), ("stills", "")],
)
async def test_h9c_lens_substreams_default_and_can_be_overridden(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mode: str, suffix: str
) -> None:
    """H9c uses the independent low-bandwidth routes without editing the original."""
    coordinator, legacy, _ = dual(hass, monkeypatch)
    coordinator.data["CAM001"]["device_sub_category"] = "CS-H9c-R100"
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    entry = coordinator.config_entry
    manager = hass.config_entries.options
    hass.config_entries.async_update_entry(
        entry,
        options={
            "cameras": {
                "CAM001": {"enc_key": "synthetic", "rtsp_stream_path": "/legacy"}
            }
        },
    )
    lenses = [
        camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
        for channel in camera_channels(coordinator.data["CAM001"])
    ]
    try:
        original = await legacy.stream_source()
        for lens, default_suffix in zip(lenses, ("102", "202"), strict=True):
            assert (await lens.stream_source()).endswith(
                f"/Streaming/Channels/{default_suffix}"
            )
        shared = await manager.async_init(entry.entry_id)
        shared = await manager.async_configure(
            shared["flow_id"], {"next_step_id": "camera_select"}
        )
        shared = await manager.async_configure(shared["flow_id"], {"serial": "CAM001"})
        shared = await manager.async_configure(
            shared["flow_id"], {"next_step_id": "camera_edit"}
        )
        assert not any(
            key.schema == "rtsp_stream_path" for key in shared["data_schema"].schema
        )
        manager.async_abort(shared["flow_id"])

        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "camera_select"}
        )
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM001"})
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "lens_streams"}
        )
        assert result["data_schema"]({}) == {
            "lens_1": "custom",
            "lens_2": "substream",
        }
        result = await manager.async_configure(
            result["flow_id"], {"lens_1": mode, "lens_2": "stills"}
        )
        if mode == "custom":
            assert result["step_id"] == "lens_stream_edit"
            assert result["data_schema"]({}) == {"lens_1": "/legacy"}
            result = await manager.async_configure(
                result["flow_id"], {"lens_1": "/Streaming/Channels/103"}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert not await lenses[1].stream_source()
        assert original.endswith("/legacy")
        if suffix:
            assert (await legacy.stream_source()).endswith(
                f"/Streaming/Channels/{suffix}"
            )
        else:
            assert not await legacy.stream_source()
            assert not legacy.supported_features & CameraEntityFeature.STREAM
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("multi_lens", [False, True])
async def test_camera_setup_reuses_original_for_first_lens(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, multi_lens: bool
) -> None:
    """Publish the original camera plus only additional lens cameras."""
    coordinator, _, _ = dual(hass, monkeypatch)
    if not multi_lens:
        coordinator.data["CAM001"]["resourceInfos"].pop()
    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"rtsp_stream_path": "/keep-original"}}}
    )
    added: list[camera.EzvizCamera] = []
    try:
        await camera.async_setup_entry(hass, entry, added.extend)
        expected = ["CAM001", "CAM001_channel_Video_1:2"] if multi_lens else ["CAM001"]
        assert [entity.unique_id for entity in added] == expected
        assert (await added[0].stream_source()).endswith("/keep-original")
    finally:
        await coordinator.async_close()


async def test_lens_warning_light_discovery_and_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the actual SDK serializer, never the sibling or the flat switch."""
    coordinator, _, sent = dual(hass, monkeypatch)

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"meta":{"code":200}}'
        return response

    monkeypatch.setattr(coordinator.ezviz_client._session, "send", send)
    data = coordinator.data["CAM001"]
    data["supportExt"]["113"] = "1"
    # Lens 2 is not supported by the first observation; an unscoped row
    # must not create an entity or stand in for its state.
    data["SWITCH"] = [
        {"type": 303, "enable": 1},
        {"type": 303, "channelNo": 1, "enable": 0},
    ]
    added = []
    await light.async_setup_entry(hass, coordinator.config_entry, added.extend)
    legacy, first = added
    assert legacy.unique_id == "CAM001_Light"
    assert first.is_on is False
    data["SWITCH"].append({"type": 303, "channelNo": 2, "enable": 1})
    coordinator.inventory_snapshot = replace(coordinator.inventory_snapshot, revision=2)
    coordinator.async_set_updated_data(coordinator.data)
    second = added[2]
    assert second.is_on is True
    assert second.unique_id == "CAM001_warning_channel_Video_1:2"
    try:
        await first.async_turn_on()
        await second.async_turn_off()
        for request, channel, enable in zip(sent, (1, 2), (1, 0), strict=True):
            assert request.method == "PUT"
            assert request.url.endswith(f"/CAM001/{channel}/{enable}/303/switchStatus")
        assert first.is_on is False
        assert second.is_on is True
        data["SWITCH"].pop()
        assert not second.available
        with pytest.raises(ServiceValidationError):
            await second.async_turn_on()
        assert len(sent) == 2
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"diskNum": 0, "diskState": "----------------"}, "no_card"),
        ({"diskNum": 1, "diskState": "0---------------"}, "normal"),
        ({"diskNum": 1, "diskState": "2---------------"}, "other"),
        ({"diskNum": 0, "diskState": "0---------------"}, None),
        ({}, None),
    ],
)
async def test_sd_card_status(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, int | str],
    expected: str | None,
) -> None:
    """Read only the current device observation, including a known empty slot."""
    coordinator, _, _ = dual(hass, monkeypatch)
    coordinator.data["CAM001"].update(supportExt={"4": "1"}, STATUS=status)
    desc = next(item for item in sensor.SENSORS if item.key == "sd_card_status")
    assert sensor._is_desc_supported(coordinator.data["CAM001"], desc)
    entity = sensor.EzvizSensor(coordinator, "CAM001", desc)
    assert entity.native_value == expected
    assert entity.available is (expected is not None)
    await coordinator.async_close()
