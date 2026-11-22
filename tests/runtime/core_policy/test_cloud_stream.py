"""Qualify opt-in cloud playback without authenticating or accessing cameras."""

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
import socket
from threading import Event
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientPayloadError, ClientSession
from aiohttp.test_utils import make_mocked_request
import av
from custom_components.ezviz_cloud import camera, cloud_stream
from custom_components.ezviz_cloud.const import (
    CONF_ENC_KEY,
    CONF_RTSP_STREAM_PATH,
    CONF_STREAM_SOURCE,
    OPTIONS_KEY_CAMERAS,
)
from pyezvizapi.client import EzvizClient
from pyezvizapi.devices.channels import camera_channels
from pyezvizapi.streaming.capture import AutoClipSource
import pytest
from yarl import URL

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.camera.const import DATA_CAMERA_PREFS
from homeassistant.components.camera.prefs import (
    CameraPreferences,
    DynamicStreamSettings,
)
from homeassistant.components.stream import Stream, create_stream
from homeassistant.components.stream.const import (
    ATTR_SETTINGS,
    ATTR_STREAMS,
    HLS_PROVIDER,
)
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS, Segment
from homeassistant.components.stream.hls import HlsMasterPlaylistView, HlsSegmentView
from homeassistant.components.stream.recorder import RecorderOutput
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from tests.runtime.core_policy import test_camera_channels
from tests.runtime.core_policy.test_camera_channels import add, dual

integration = test_camera_channels.integration

_LOOPBACK_CONNECT = socket.socket.connect


@pytest.mark.parametrize("change", ["save", "conflict", "missing", "stopping"])
async def test_playback_option_preserves_rtsp_settings(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The native flow reloads and changes only the selected camera's playback option."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    baseline = {"CAM001": {CONF_RTSP_STREAM_PATH: "/saved"}, "OTHER": {"other": True}}
    hass.config_entries.async_update_entry(
        entry, options={OPTIONS_KEY_CAMERAS: baseline}
    )
    manager = hass.config_entries.options
    try:
        result = await manager.async_init(entry.entry_id)
        flow_id = result["flow_id"]
        for submission in (
            {"next_step_id": "camera_select"},
            {"serial": "CAM001"},
            {"next_step_id": "playback_source"},
        ):
            result = await manager.async_configure(flow_id, submission)
        assert result["step_id"] == "playback_source"
        if change == "stopping":
            coordinator.begin_shutdown()
        elif change == "missing":
            coordinator.data.pop("CAM001")
        elif change == "conflict":
            hass.config_entries.async_update_entry(
                entry,
                options={
                    OPTIONS_KEY_CAMERAS: {
                        **baseline,
                        "CAM001": {
                            **baseline["CAM001"],
                            CONF_STREAM_SOURCE: "cloud_main",
                        },
                    }
                },
            )
        result = await manager.async_configure(
            flow_id, {CONF_STREAM_SOURCE: "cloud_standard"}
        )
        if change in ("missing", "stopping"):
            assert result["type"] is FlowResultType.ABORT
            reload.assert_not_awaited()
            return
        if change == "conflict":
            assert result["errors"] == {"base": "options_changed"}
            assert (
                entry.options[OPTIONS_KEY_CAMERAS]["CAM001"][CONF_STREAM_SOURCE]
                == "cloud_main"
            )
            result = await manager.async_configure(
                flow_id, {CONF_STREAM_SOURCE: "cloud_standard"}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[OPTIONS_KEY_CAMERAS] == {
            **baseline,
            "CAM001": {
                CONF_RTSP_STREAM_PATH: "/saved",
                CONF_STREAM_SOURCE: "cloud_standard",
            },
        }
        reload.assert_awaited_once_with(entry.entry_id)
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("lens_index", "main_quality"), [(0, False), (1, False), (2, False), (1, True)]
)
async def test_cloud_stream_lifecycle(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    lens_index: int,
    main_quality: bool,
) -> None:
    """Core owns HLS while the loopback connection owns the exact SDK reader."""
    coordinator, legacy, _ = dual(hass, monkeypatch)
    assert (await legacy.stream_source()).startswith("rtsp://")
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={
            OPTIONS_KEY_CAMERAS: {
                "CAM001": {
                    CONF_STREAM_SOURCE: (
                        "cloud_main" if main_quality else "cloud_standard"
                    ),
                    CONF_ENC_KEY: "synthetic-media-key",
                    CONF_RTSP_STREAM_PATH: None,
                }
            }
        },
    )
    coordinator.data["CAM001"]["encrypted"] = True
    if lens_index == 0:
        coordinator.data["CAM001"]["resourceInfos"] = [
            {"resourceIdentifier": "Video", "localIndex": 0}
        ]
    channels = camera_channels(coordinator.data["CAM001"])
    target = (
        camera.EzvizCamera(hass, coordinator, "CAM001", "", "", None)
        if lens_index != 2
        else camera.EzvizChannelCamera(hass, coordinator, "CAM001", channels[1])
    )
    hass.config.components.add("stream")
    hass.data[DATA_CAMERA_PREFS] = CameraPreferences(hass)
    await hass.data[DATA_CAMERA_PREFS].async_load()
    hass.data["stream"] = {ATTR_SETTINGS: STREAM_SETTINGS_NON_LL_HLS, ATTR_STREAMS: []}
    closed = Event()
    readers = []

    class Reader:
        """Block after one chunk until the stream owner closes us."""

        def __init__(self) -> None:
            self.first = True

        def read(self) -> bytes:
            if self.first:
                self.first = False
                return b"synthetic transport stream"
            assert closed.wait(5)
            return b""

        def close(self) -> None:
            closed.set()

    def open_reader(
        client: EzvizClient,
        serial: str,
        *,
        source: AutoClipSource,
        channel: int,
        ffmpeg_path: str,
        media_key: str | None,
        cloud_stream_index: int,
    ) -> Reader:
        assert client is not coordinator.ezviz_client
        assert client.feature_code == coordinator.ezviz_client.feature_code
        assert serial == "CAM001"
        assert channel == lens_index
        assert source.mode == "cloud"
        assert ffmpeg_path
        assert cloud_stream_index == (1 if main_quality else 2)
        assert media_key == "synthetic-media-key"
        readers.append(Reader())
        return readers[-1]

    monkeypatch.setattr(cloud_stream, "open_live_mpegts_stream", open_reader)
    platform = await add(hass, target, "camera")
    try:
        assert target.supported_features == CameraEntityFeature.STREAM
        assert await target.stream_source() is None
        stream = await target.async_create_stream()
        assert stream is not None
        assert await target.async_create_stream() is stream
        assert not readers
        output = stream.add_provider(HLS_PROVIDER)
        start = Mock(wraps=output.idle_timer.start)
        monkeypatch.setattr(output.idle_timer, "start", start)
        source = URL(stream.source)
        assert source.host == "127.0.0.1"

        def connect(sock: socket.socket, address: tuple[str, int]) -> None:
            assert address == (source.host, source.port)
            _LOOPBACK_CONNECT(sock, address)

        monkeypatch.setattr(socket.socket, "connect", connect)
        async with (
            asyncio.timeout(6),
            ClientSession() as session,
            session.get(source.with_query(None)) as denied,
        ):
            assert denied.status == 404
            assert not readers
            async with session.get(source) as response:
                assert (
                    await response.content.readexactly(26)
                    == b"synthetic transport stream"
                )
                async with session.get(source) as duplicate:
                    assert duplicate.status == 409
                assert len(readers) == 1
                start.assert_called()
                if lens_index == 2:
                    coordinator.data["CAM001"]["resourceInfos"] = [
                        {"resourceIdentifier": "Video", "localIndex": 1}
                    ]
                    target._handle_coordinator_update()
                    with pytest.raises(ClientPayloadError):
                        await response.content.read()
                    async with session.get(source) as missing:
                        assert missing.status == 503
                await platform.async_reset()
                assert closed.is_set()
    finally:
        closed.set()
        await platform.async_reset()
        await coordinator.async_close()


async def test_cloud_primary_channel_zero_is_provisional(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Move the primary to channel 1 rather than sharing the new secondary."""
    coordinator, _, _ = dual(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={OPTIONS_KEY_CAMERAS: {"CAM001": {CONF_STREAM_SOURCE: "cloud_main"}}},
    )
    device = coordinator.data["CAM001"]
    zero = {"resourceIdentifier": "Video", "localIndex": 0}
    one = {"resourceIdentifier": "Video", "localIndex": 1}
    device["resourceInfos"] = [zero]
    primary = camera.EzvizCamera(hass, coordinator, "CAM001", "", "", None)
    try:
        assert primary._cloud_route() == cloud_stream.CloudStreamRoute(0, 1, None)

        device["resourceInfos"] = [zero, one]
        zero_channel = next(ch for ch in camera_channels(device) if ch.channel_no == 0)
        secondary = camera.EzvizChannelCamera(hass, coordinator, "CAM001", zero_channel)
        assert primary._cloud_route() == cloud_stream.CloudStreamRoute(1, 1, None)
        assert secondary._cloud_route() == cloud_stream.CloudStreamRoute(0, 1, None)

        device["resourceInfos"] = [zero]
        assert primary._cloud_route() is None
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("seen_at_setup", [False, True])
async def test_cloud_primary_remembers_inventory_before_playback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, seen_at_setup: bool
) -> None:
    """Channel 0 must not take over a previously observed primary."""
    coordinator, _, _ = dual(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={OPTIONS_KEY_CAMERAS: {"CAM001": {CONF_STREAM_SOURCE: "cloud_main"}}},
    )
    device = coordinator.data["CAM001"]
    zero = {"resourceIdentifier": "Video", "localIndex": 0}
    one = {"resourceIdentifier": "Video", "localIndex": 1}
    device["resourceInfos"] = [zero, one] if seen_at_setup else [zero]
    primary = camera.EzvizCamera(hass, coordinator, "CAM001", "", "", None)
    platform = await add(hass, primary, "camera")
    try:
        if not seen_at_setup:
            device["resourceInfos"] = [zero, one]
            coordinator.async_update_listeners()
        device["resourceInfos"] = [zero]
        coordinator.async_update_listeners()
        assert await primary.async_create_stream() is None
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("startup", ["recorder_only", "empty_hls", "hls_lookback"])
async def test_cold_cloud_recording_expires_and_can_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: str
) -> None:
    """A cold recorder releases its provider without arming its duration timer."""
    coordinator, _, _ = dual(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={OPTIONS_KEY_CAMERAS: {"CAM001": {CONF_STREAM_SOURCE: "cloud_main"}}},
    )
    target = camera.EzvizCamera(hass, coordinator, "CAM001", "", "", None)
    hass.config.components.add("stream")
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    hass.data[DATA_CAMERA_PREFS] = CameraPreferences(hass)
    await hass.data[DATA_CAMERA_PREFS].async_load()
    hass.data["stream"] = {ATTR_SETTINGS: STREAM_SETTINGS_NON_LL_HLS, ATTR_STREAMS: []}
    monkeypatch.setattr(camera, "CLOUD_RECORDING_STARTUP_TIMEOUT", 0.05)

    def run_worker(stream: Stream) -> None:
        stream._thread_quit.wait()

    monkeypatch.setattr(Stream, "_run_worker", run_worker)
    platform = await add(hass, target, "camera")
    try:
        stream = await target.async_create_stream()
        assert stream is not None
        expected = {}
        if startup != "recorder_only":
            stream.dynamic_stream_settings = DynamicStreamSettings(preload_stream=True)
            expected[HLS_PROVIDER] = stream.add_provider(HLS_PROVIDER)
            await stream.start()
        lookback_seen = asyncio.Event()

        async def record_lookback(recorder: RecorderOutput) -> None:
            assert recorder.last_segment is not None
            lookback_seen.set()
            await recorder.recv()

        if startup == "hls_lookback":
            monkeypatch.setattr(RecorderOutput, "async_record", record_lookback)

        for _ in range(2):
            if startup == "hls_lookback":
                lookback_seen.clear()
                hls = expected[HLS_PROVIDER]
                # Publish HLS history and wake Core's lookback wait, but never
                # publish a fresh segment to the recorder itself.
                for sequence in range(2):
                    segment = Segment(sequence, b"", 0, datetime.now(UTC), ())
                    hass.loop.call_soon(hls._async_put, segment)
            async with asyncio.timeout(2):
                with pytest.raises(HomeAssistantError, match="recording did not start"):
                    await stream.async_record(str(tmp_path / "cold.mp4"), duration=60)
            if startup == "hls_lookback":
                assert lookback_seen.is_set()
            assert stream.outputs() == expected
            if startup != "recorder_only":
                assert stream._thread is not None and stream._thread.is_alive()
            else:
                assert stream._thread is None
    finally:
        await platform.async_reset()
        await coordinator.async_close()


def _synthetic_mpegts() -> bytes:
    """Generate H.264 GOPs using the same PyAV profile as the native suite."""
    output = BytesIO()
    with av.open(output, mode="w", format="mpegts") as muxer:
        video = muxer.add_stream("libx264", rate=10)
        video.width = 96
        video.height = 64
        video.pix_fmt = "yuv420p"
        video.options = {"g": "10", "preset": "ultrafast", "tune": "zerolatency"}
        frame = av.VideoFrame(96, 64, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for index in range(300):
            frame.pts = index
            muxer.mux(video.encode(frame))
        muxer.mux(video.encode())
    return output.getvalue()


async def test_core_hls_publishes_cloud_relay_segments(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's real PyAV worker can consume the relay and publish an HLS segment."""
    coordinator, _, _ = dual(hass, monkeypatch)
    payload = await hass.async_add_executor_job(_synthetic_mpegts)
    closed = Event()

    class Reader:
        """Keep the synthetic source connected until Core stops reading."""

        def __init__(self) -> None:
            self.body = BytesIO(payload)

        def read(self) -> bytes:
            if chunk := self.body.read(65536):
                return chunk
            assert closed.wait(10)
            return b""

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(
        cloud_stream, "open_live_mpegts_stream", lambda *_a, **_kw: Reader()
    )
    hass.config.components.add("stream")
    hass.data["stream"] = {ATTR_SETTINGS: STREAM_SETTINGS_NON_LL_HLS, ATTR_STREAMS: []}
    relay = cloud_stream.CloudStreamRelay(
        hass,
        coordinator.ezviz_client,
        "CAM001",
        "ffmpeg",
        lambda: cloud_stream.CloudStreamRoute(1, 1, None),
    )
    source = await relay.async_start()
    stream = create_stream(hass, source, {}, DynamicStreamSettings())
    hls_output = stream.add_provider(HLS_PROVIDER)
    try:
        async with asyncio.timeout(8):
            request = make_mocked_request("GET", "/master_playlist.m3u8")
            # Core waits for a playable segment, not just a newly announced one.
            playlist = await HlsMasterPlaylistView().handle(request, stream, "", "")
            assert playlist.status == 200
            assert b"playlist.m3u8" in playlist.body
            sequence = str(hls_output.sequences[-2])
            segment = await HlsSegmentView().handle(request, stream, sequence, "")
            assert segment.status == 200
            assert segment.content_type == "video/iso.segment"
            assert segment.body
    finally:
        await relay.async_stop()
        await stream.stop()
        hls_output.cleanup()
        await coordinator.async_close()
    assert closed.is_set()
