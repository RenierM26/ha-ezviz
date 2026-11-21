"""Native HA channel targets, isolated captures, images, options and notifications."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
import logging
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
    media_source as media,
    views,
)
from custom_components.ezviz_cloud.channels import (
    CONF_LENS_RTSP_PATHS,
    async_add_channel_entities,
)
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
import httpx
from PIL import Image
from pyezvizapi.devices.channels import camera_channels
from pyezvizapi.devices.snapshot import CameraState, DeviceSnapshot
import pytest
import requests

from homeassistant import loader
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.media_source import Unresolvable
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.core_policy.test_ptz_position import camera_target


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
    coordinator, legacy, sent = dual(hass, monkeypatch)
    channels = camera_channels(coordinator.data["CAM001"])
    target = camera.EzvizChannelCamera(hass, coordinator, "CAM001", channels[1])
    snapshots = [
        image.EzvizChannelSnapshot(hass, coordinator, "CAM001", {}, c) for c in channels
    ]
    old = image.EzvizSnapshot(hass, coordinator, "CAM001", {})
    platforms = [await add(hass, target, "camera")]
    platforms += [await add(hass, entity, "image") for entity in [*snapshots, old]]

    def send(method: str, url: str, **_kwargs: object) -> requests.Response:
        request = requests.Request(method, url).prepare()
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"meta":{"code":200},"captureInfo":{"picUrl":"https://images.example.invalid/lens2"}}'
        return response

    monkeypatch.setattr(coordinator.ezviz_client._session, "request", send)
    try:
        result = await target.async_capture_picture()
        await hass.async_block_till_done()
        assert len(sent) == 1 and sent[0].url.endswith("/CAM001/2/capture")
        assert result["picture_url"].endswith("/lens2")
        assert snapshots[0].image_url is None and old.image_url is None
        assert snapshots[1].image_url.endswith("/lens2")
        assert "images.example.invalid" not in json.dumps(
            hass.states.get(snapshots[1].entity_id).as_dict(), default=str
        )
        assert legacy.unique_id == "CAM001" and old.unique_id == "CAM001_snapshot"
        coordinator.begin_shutdown()
        with pytest.raises(HomeAssistantError):
            await target.async_capture_picture()
        assert len(sent) == 1
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
        "ffmpeg_arguments": "/legacy",
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


def jpeg(color: str) -> bytes:
    """Create distinguishable valid synthetic frames."""
    with BytesIO() as output, Image.new("RGB", (8, 8), color) as frame:
        frame.save(output, format="JPEG")
        return output.getvalue()


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
            result["flow_id"], {"lens_1": "stills", "lens_2": "custom"}
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
            result["flow_id"], {"lens_1": "custom", "lens_2": "custom"}
        )
        result = await manager.async_configure(
            result["flow_id"], {"lens_1": "/concurrent", "lens_2": "/second"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM001"][CONF_LENS_RTSP_PATHS] == {
            "Video:1": "/concurrent",
            "Video_1:2": "/second",
        }
    finally:
        await coordinator.async_close()


async def test_lens_options_abort_when_no_reported_channels_remain(
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
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "no_camera_channels"
    finally:
        await coordinator.async_close()


async def test_existing_channel_registry_identity_restores_a_remaining_lens(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restarting with one missing lens does not suppress the other known channel."""
    coordinator, _, _ = dual(hass, monkeypatch)
    entry = coordinator.config_entry
    er.async_get(hass).async_get_or_create(
        "camera", DOMAIN, "CAM001_channel_Video:1", config_entry=entry
    )
    coordinator.data["CAM001"]["resourceInfos"] = coordinator.data["CAM001"][
        "resourceInfos"
    ][:1]
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
        assert len(added) == 1 and added[0].unique_id == "CAM001_channel_Video:1"
        assert added[0].available
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
                    "ffmpeg_arguments": "/legacy",
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
        if path:
            result = await manager.async_configure(
                result["flow_id"],
                {"lens_1": "/lens1", "lens_2": "rtsp://private.invalid/credentials"},
            )
            assert result["errors"] == {"lens_2": "invalid_rtsp_path"}
        custom = {"lens_1": "/lens1"}
        if path:
            custom["lens_2"] = path
        result = await manager.async_configure(result["flow_id"], custom)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        options = entry.options["cameras"]["CAM001"]
        assert options["enc_key"] == "keep" and options["unknown"] == 7
        assert options["ffmpeg_arguments"] == "/legacy"
        assert options[CONF_LENS_RTSP_PATHS].get("Video_1:2", "") == path
        assert options[CONF_LENS_RTSP_PATHS]["Video:1"] == "/lens1"
        reload_entry.assert_awaited_once_with(entry.entry_id)
    finally:
        await coordinator.async_close()


async def test_h9c_lens_substreams_default_and_can_be_overridden(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
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
                "CAM001": {"enc_key": "synthetic", "ffmpeg_arguments": "/legacy"}
            }
        },
    )
    lenses = [
        camera.EzvizChannelCamera(hass, coordinator, "CAM001", channel)
        for channel in camera_channels(coordinator.data["CAM001"])
    ]
    try:
        original = await legacy.stream_source()
        for lens, suffix in zip(lenses, ("102", "202"), strict=True):
            assert (await lens.stream_source()).endswith(
                f"/Streaming/Channels/{suffix}"
            )
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "camera_select"}
        )
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM001"})
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "lens_streams"}
        )
        assert result["data_schema"]({}) == {
            "lens_1": "substream",
            "lens_2": "substream",
        }
        result = await manager.async_configure(
            result["flow_id"], {"lens_1": "main_stream", "lens_2": "stills"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert (await lenses[0].stream_source()).endswith("/Streaming/Channels/101")
        assert not await lenses[1].stream_source()
        assert await legacy.stream_source() == original
    finally:
        await coordinator.async_close()
