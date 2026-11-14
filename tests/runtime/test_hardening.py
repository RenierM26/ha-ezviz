"""Actual HA image CPU ownership, capture cleanup and durable storage controls."""

import asyncio
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import atomicwrites
import httpx
from PIL import Image as PILImage
from pyezvizapi import DeviceSnapshot
from pyezvizapi.devices.snapshot import CameraState, DeviceHealth
import pytest

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import (
    camera,
    config_flow,
    coordinator,
    image,
    mqtt,
    runtime,
    tokens,
    views,
)
from custom_components.ezviz_cloud.const import (
    ATTR_TYPE_CLOUD,
    CONF_SDK_TOKEN,
    DOMAIN,
    REGION_EU,
)
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState, current_entry
from homeassistant.core import EVENT_HOMEASSISTANT_STOP, CoreState
from homeassistant.exceptions import HomeAssistantError


def entry_for(hass, *, state=ConfigEntryState.NOT_LOADED):
    """Construct a real entry; only test registration mirrors HA's test helper."""
    token = {
        "session_id": "synthetic",
        "rf_session_id": "refresh",
        "user_id": "user",
        "username": "user",
        "api_url": "api.example.invalid",
    }
    entry = ConfigEntry(
        data={
            "type": ATTR_TYPE_CLOUD,
            "session_id": "synthetic",
            "rf_session_id": "refresh",
            "user_id": "user",
            "url": "api.example.invalid",
            CONF_SDK_TOKEN: token,
        },
        discovery_keys={},
        domain=DOMAIN,
        minor_version=1,
        options={"cameras": {}},
        source=SOURCE_USER,
        state=state,
        subentries_data=(),
        title="Synthetic",
        unique_id=None,
        version=4,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


def entities(hass, *, lens_total=1):
    entry = entry_for(hass)
    api = SimpleNamespace(
        capture_picture=MagicMock(
            return_value={"captureInfo": {"picUrl": "https://images.example.invalid/capture"}}
        ),
        close_session=MagicMock(),
    )
    context = current_entry.set(entry)
    try:
        data = coordinator.EzvizDataUpdateCoordinator(
            hass, config_entry=entry, api=api, api_timeout=5
        )
    finally:
        current_entry.reset(context)
    data.data = {
        "CAM001": {
            "name": "Synthetic",
            "device_category": "IPC",
            "device_sub_category": "CP8",
            "version": "1",
            "status": 1,
            "local_ip": "192.0.2.1",
            "local_rtsp_port": 554,
            "last_alarm_pic": "https://images.example.invalid/alarm",
            "last_alarm_time": "2026-11-08T12:00:00+00:00",
        }
    }
    now = datetime.now(UTC)
    data.inventory_snapshot = DeviceSnapshot(
        1,
        now,
        {"CAM001": CameraState("CAM001", "camera", data.data["CAM001"])},
        {"CAM001": DeviceHealth(True, True, now)},
    )
    hass.data[DATA_FFMPEG] = SimpleNamespace(binary="/usr/bin/ffmpeg")
    cam = camera.EzvizCamera(hass, data, "CAM001", "synthetic", "synthetic", "/stream")
    snap = image.EzvizSnapshot(hass, data, "CAM001", {})
    alarm = image.EzvizLastMotion(
        hass, data, "CAM001", {}, lens_index=lens_total, lens_total=lens_total
    )
    for entity, entity_id in [
        (cam, "camera.synthetic"),
        (snap, "image.synthetic_snapshot"),
        (alarm, "image.synthetic_motion"),
    ]:
        entity.hass = hass
        entity.entity_id = entity_id
        entity.async_write_ha_state = MagicMock()
    return entry, api, data, cam, snap, alarm


async def test_real_decryption_and_pil_crop_run_off_the_event_loop(hass, monkeypatch):
    _, _, data, _, _, alarm = entities(hass, lens_total=2)
    hass.config_entries.async_update_entry(
        data.config_entry, options={"cameras": {alarm._serial: {"enc_key": "SYNTHETIC"}}}
    )
    buf = BytesIO()
    PILImage.new("RGB", (16, 32), (20, 80, 40)).save(buf, format="JPEG")
    monkeypatch.setattr(
        alarm, "_fetch_url", AsyncMock(return_value=httpx.Response(200, content=buf.getvalue()))
    )
    loop_thread, threads = get_ident(), []
    decrypt, split = image.decrypt_image, image._split_composite_image

    def decrypt_on_worker(*args):
        threads.append(get_ident())
        return decrypt(*args)

    def split_on_worker(*args):
        threads.append(get_ident())
        return split(*args)

    monkeypatch.setattr(image, "decrypt_image", decrypt_on_worker)
    monkeypatch.setattr(image, "_split_composite_image", split_on_worker)
    result = await alarm.async_image()
    assert PILImage.open(BytesIO(result)).size == (16, 16)
    assert len(threads) == 2 and all(thread != loop_thread for thread in threads)
    await data.async_close()


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_capture_cleanup_owns_worker_and_rejects_late_dispatch(
    hass, monkeypatch, cancel_caller
):
    _, api, data, cam, _, _ = entities(hass)
    entered, release = Event(), Event()
    order = []

    def capture(*_args):
        entered.set()
        assert release.wait(5)
        order.append("capture")
        return {"captureInfo": {"picUrl": "https://images.example.invalid/late"}}

    api.capture_picture = capture
    api.close_session.side_effect = lambda: order.append("close")
    dispatch = MagicMock()
    monkeypatch.setattr(camera, "async_dispatcher_send", dispatch)
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.001)
    owner = runtime.RuntimeCleanup(hass, data)
    task = asyncio.create_task(cam.async_capture_picture())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel_caller:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await owner.async_stop()
        assert order == [] and not owner.task.done()
        with pytest.raises(HomeAssistantError, match="shutting down"):
            await data.async_execute(capture)
    finally:
        release.set()
    if not cancel_caller:
        with pytest.raises(HomeAssistantError, match="shutting down"):
            await task
    await owner.async_wait_stopped()
    assert order == ["capture", "close"]
    dispatch.assert_not_called()
    await owner.async_wait_stopped()
    api.close_session.assert_called_once()


async def test_cancelled_image_cpu_work_remains_owned_until_http_close(hass, monkeypatch):
    _, api, data, _, snap, _ = entities(hass)
    entered, release = Event(), Event()
    order = []

    def process(*_args):
        entered.set()
        assert release.wait(5)
        order.append("image")
        return b"processed"

    monkeypatch.setattr(image, "_process_image_bytes", process)
    monkeypatch.setattr(
        snap, "_fetch_url", AsyncMock(return_value=httpx.Response(200, content=b"input"))
    )
    snap._handle_picture_captured("https://images.example.invalid/capture")
    api.close_session.side_effect = lambda: order.append("close")
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.001)
    owner = runtime.RuntimeCleanup(hass, data)
    task = asyncio.create_task(snap.async_image())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.async_stop()
        assert not order and not owner.task.done()
        assert await snap.async_image() is None
    finally:
        release.set()
    await owner.async_wait_stopped()
    assert order == ["image", "close"]
    assert snap._cached_image is None


async def test_last_motion_generation_discards_older_alarm_download(hass, monkeypatch):
    _, _, data, _, _, alarm = entities(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        if url.endswith("/alarm"):
            entered.set()
            await release.wait()
            return httpx.Response(200, content=b"old")
        return httpx.Response(200, content=b"new")

    monkeypatch.setattr(alarm, "_fetch_url", fetch)
    old = asyncio.create_task(alarm.async_image())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        data.data["CAM001"]["last_alarm_pic"] = "https://images.example.invalid/new"
        alarm._handle_coordinator_update()
    finally:
        release.set()
    assert await old is None
    assert await alarm.async_image() == b"new"
    assert alarm.unique_id == "CAM001_last_motion_image"
    await data.async_close()


async def test_native_storage_durability_and_private_mode(hass, monkeypatch):
    owner = tokens.TokenPersistence(hass, entry_for(hass))
    entered, release = Event(), Event()
    sync, calls = atomicwrites._proper_fsync, []

    def fsync(fd):
        calls.append(fd)
        entered.set()
        assert release.wait(5)
        sync(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", fsync)
    token = {"session_id": "synthetic-new", "push_state": {"counter": 2}}
    task = asyncio.ensure_future(hass.async_add_executor_job(owner.save_from_worker, token))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
    finally:
        release.set()
    await task
    path = Path(owner._store.path)
    assert path.stat().st_mode & 0o077 == 0
    assert json.loads(path.read_text())["data"]["token"] == token
    assert len(calls) >= 2
    await owner.async_remove()
    assert not path.exists()


async def test_native_storage_failure_is_not_acknowledged(hass, monkeypatch):
    owner = tokens.TokenPersistence(hass, entry_for(hass))

    def fail(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(atomicwrites, "_proper_fsync", fail)
    with pytest.raises(OSError):
        await hass.async_add_executor_job(owner.save_from_worker, {"session_id": "synthetic"})


async def test_native_shutdown_deferred_save_is_not_acknowledged(hass):
    owner = tokens.TokenPersistence(hass, entry_for(hass))
    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(OSError):
            await hass.async_add_executor_job(owner.save_from_worker, {"session_id": "synthetic"})
    finally:
        hass.set_state(CoreState.running)
        await owner.async_remove()


async def test_actual_ha_stop_awaits_registered_integration_cleanup(hass, monkeypatch):
    entry = entry_for(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
    observed = datetime.now(UTC)
    api = SimpleNamespace(
        login=lambda: None,
        export_token=lambda: dict(entry.data[CONF_SDK_TOKEN]),
        poll_devices=lambda _options: DeviceSnapshot(1, observed, {}, {}),
        close_session=MagicMock(),
    )
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", lambda **_kwargs: api)
    monkeypatch.setattr(
        ezviz_cloud,
        "EzvizMqttHandler",
        lambda *_args: SimpleNamespace(async_start=lambda: None, async_wait_stopped=AsyncMock()),
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    # Isolate HTTP exposure/DNS setup; exercise the real integration stop hook.
    monkeypatch.setattr(ezviz_cloud, "ImageProxyView", lambda _hass: object())
    hass.http = SimpleNamespace(register_view=lambda _view: None)
    context = current_entry.set(entry)
    try:
        assert await ezviz_cloud.async_setup_entry(hass, entry)
    finally:
        current_entry.reset(context)
    owner = entry.runtime_data
    assert isinstance(owner, runtime.RuntimeCleanup)
    assert entry.entry_id not in hass.data[DOMAIN]
    entered, release = Event(), Event()
    order = []

    def work():
        entered.set()
        assert release.wait(5)
        order.append("work")

    api.close_session.side_effect = lambda: order.append("close")
    task = asyncio.create_task(owner.coordinator.async_execute(work))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.001)
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert order == ["work", "close"]
    assert owner.task.done() and not owner.task.cancelled()
    api.close_session.assert_called_once()


async def test_key_update_retires_inflight_processing_with_immutable_inputs(hass, monkeypatch):
    _, _, data, _, _, alarm = entities(hass)
    alarm.alarm_image_password = "OLD"
    key_entry = entry_for(hass)
    data.config_entry = key_entry
    hass.config_entries.async_update_entry(
        key_entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    entered, release = Event(), Event()
    passwords = []

    def process(_body, password, *_lens):
        passwords.append(password)
        if password == "OLD":
            entered.set()
            assert release.wait(5)
            return b"old"
        return b"new"

    monkeypatch.setattr(image, "_process_image_bytes", process)
    monkeypatch.setattr(
        alarm, "_fetch_url", AsyncMock(return_value=httpx.Response(200, content=b"input"))
    )
    task = asyncio.create_task(alarm.async_image())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        hass.config_entries.async_update_entry(
            key_entry, options={"cameras": {alarm._serial: {"enc_key": "NEW"}}}
        )
        alarm._handle_coordinator_update()
    finally:
        release.set()
    assert await task is None
    assert await alarm.async_image() == b"new"
    assert passwords == ["OLD", "NEW"]
    await data.async_close()


async def test_proxy_decryption_runs_off_loop_and_never_forwards_ha_auth(hass, monkeypatch):

    entry, _, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "SYNTHETIC"}}}
    )
    entry.runtime_data = SimpleNamespace(coordinator=data)
    response = SimpleNamespace(
        status=200,
        content_type="image/jpeg",
        read=AsyncMock(return_value=b"input"),
        release=MagicMock(),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=response))
    monkeypatch.setattr(views, "async_get_clientsession", lambda _hass: session)
    threads = []

    def decrypt(body, key):
        threads.append(get_ident())
        assert body == b"input" and key == "SYNTHETIC"
        return b"decrypted"

    monkeypatch.setattr(views, "decrypt_image", decrypt)
    proxy = views.ImageProxyView(hass)
    url = urlsafe_b64encode(b"https://images.example.invalid/synthetic").decode()
    result = await proxy.get(
        SimpleNamespace(headers={"Authorization": "PRIVATE"}), entry.entry_id, "CAM001", url
    )
    assert result.status == 200 and result.body == b"decrypted"
    assert threads and threads[0] != get_ident()
    assert "Authorization" not in session.get.call_args.kwargs["headers"]
    response.release.assert_called_once()
    await data.async_close()


@pytest.mark.parametrize("phase", ["connect", "health"])
async def test_ha_stop_drains_cancelled_push_monitor_workers(hass, monkeypatch, phase):
    entry, api, data, _, _, _ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    entered, release = Event(), Event()
    order = []

    def work():
        entered.set()
        assert release.wait(5)
        order.append(phase)

    sdk = SimpleNamespace(
        connect=work if phase == "connect" else lambda: None,
        raise_if_failed=work if phase == "health" else lambda: None,
        stop=lambda: order.append("disconnect"),
    )
    api.get_mqtt_client = lambda **_kwargs: sdk
    api.close_session.side_effect = lambda: order.append("close")
    monkeypatch.setattr(mqtt, "PUSH_HEALTH_SECONDS", 0.001)
    handler = mqtt.EzvizMqttHandler(hass, api, entry.entry_id)
    owner = runtime.RuntimeCleanup(hass, data, push=handler)

    async def shutdown(_event):
        await owner.async_wait_stopped()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    handler.async_start()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
        await owner.async_wait_stopped()
    assert order == [phase, "disconnect", "close"]
    assert not handler._worker_futures


async def test_ha_stop_keeps_actual_fsync_owned_after_producer_cancel(hass, monkeypatch):
    owner = tokens.TokenPersistence(hass, entry_for(hass))
    entered, release, finished = Event(), Event(), Event()
    original = atomicwrites._proper_fsync

    def fsync(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", fsync)
    token = {"session_id": "synthetic-final"}

    def save():
        try:
            owner.save_from_worker(token)
        finally:
            finished.set()

    async def produce():
        await hass.async_add_executor_job(save)

    producer = hass.async_create_background_task(produce(), "Synthetic background producer")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
        await asyncio.gather(producer, return_exceptions=True)
    assert await asyncio.to_thread(finished.wait, 2)
    await owner.async_drain()
    assert json.loads(Path(owner._store.path).read_text())["data"]["token"] == token


async def test_cancelled_credential_flow_retains_client_cleanup_through_ha_stop(hass, monkeypatch):
    entered, release = Event(), Event()
    order = []

    def login():
        entered.set()
        assert release.wait(5)
        order.append("login")
        return {"session_id": "synthetic"}

    client = SimpleNamespace(enable_channel99=login, close_session=lambda: order.append("close"))
    monkeypatch.setattr(config_flow, "EzvizClient", lambda **_kwargs: client)
    flow = config_flow.EzvizConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = DOMAIN
    task = asyncio.create_task(
        flow.async_step_user(
            {
                "username": "synthetic@example.invalid",
                "password": "SYNTHETIC",
                "region": REGION_EU,
                "timeout": 5,
            }
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert order == ["login", "close"]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_completed_critical_work_removes_temporary_stop_listeners(hass):
    _, _, data, _, _, _ = entities(hass)
    before = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)
    for _ in range(3):
        assert await data.async_execute(lambda: 42) == 42
        assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0) == before
    await data.async_close()
    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0) == before


@pytest.mark.parametrize("stage", ["login", "poll", "platforms"])
async def test_stop_during_initial_setup_cannot_complete_or_start_push(hass, monkeypatch, stage):
    entry = entry_for(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
    entered, release, platform_release = Event(), Event(), asyncio.Event()
    order = []

    def work(label):
        if stage == label:
            entered.set()
            assert release.wait(5)
            order.append(label)

    def poll(_options):
        work("poll")
        return DeviceSnapshot(1, datetime.now(UTC), {}, {})

    api = SimpleNamespace(
        login=lambda: work("login"),
        export_token=lambda: dict(entry.data[CONF_SDK_TOKEN]),
        poll_devices=poll,
        close_session=MagicMock(side_effect=lambda: order.append("close")),
    )
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", lambda **_kwargs: api)
    push = SimpleNamespace(async_start=MagicMock(), async_wait_stopped=AsyncMock())
    monkeypatch.setattr(ezviz_cloud, "EzvizMqttHandler", lambda *_args: push)

    async def forward_platforms(*_args):
        if stage == "platforms":
            entered.set()
            await platform_release.wait()
            order.append("platforms")

    forward = AsyncMock(side_effect=forward_platforms)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(ezviz_cloud, "ImageProxyView", lambda _hass: object())
    hass.http = SimpleNamespace(register_view=lambda _view: None)
    context = current_entry.set(entry)
    try:
        task = asyncio.create_task(ezviz_cloud.async_setup_entry(hass, entry))
    finally:
        current_entry.reset(context)
    try:
        assert await asyncio.to_thread(entered.wait, 2)

        def resume():
            release.set()
            platform_release.set()

        hass.loop.call_later(0.05, resume)
        await hass.async_stop()
    finally:
        release.set()
        platform_release.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
    if stage == "platforms":
        forward.assert_called_once()
        assert order == ["close", "platforms"]
    else:
        forward.assert_not_called()
        assert order == [stage, "close"]
    push.async_start.assert_not_called()
    assert isinstance(outcome[0], Exception)
    api.close_session.assert_called_once()
