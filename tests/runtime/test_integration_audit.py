"""Offline regression coverage for whole-integration audit findings."""

import asyncio
from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import logging
from struct import pack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from Crypto.Cipher import AES
from PIL import Image
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
import pytest

from custom_components.ezviz_cloud import light, siren, switch, views
from custom_components.ezviz_cloud.image import EzvizLastMotion, httpx
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform

from .test_hardening import entities


def jpeg():
    with BytesIO() as output, Image.new("RGB", (12, 8)) as picture:
        picture.save(output, "JPEG")
        return output.getvalue()


def proxy_fixture(hass, monkeypatch, body):
    entry, _api, data, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    response = SimpleNamespace(status=200, url="https://images.example.invalid/image", read=AsyncMock(return_value=body), release=MagicMock())
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(views, "ClientSession", lambda **kwargs: session)
    return entry, data, views.ImageProxyView(hass), session, response


@pytest.mark.parametrize("target", ["http://127.0.0.1/a", "https://127.0.0.1/a", "https://192.168.1.1/a", "https://[::1]/a", "https://127.1/a", "https://2130706433/a", "https://untrusted.example/a"])
async def test_caller_urls_and_unknown_camera_never_fetch(hass, monkeypatch, target):
    entry, data, proxy, session, _ = proxy_fixture(hass, monkeypatch, jpeg())
    supplied = urlsafe_b64encode(target.encode()).decode()
    assert (await proxy.get(None, entry.entry_id, "CAM001", supplied)).status == 404
    reference = data.register_image("CAM001", target)
    assert (await proxy.get(None, entry.entry_id, "UNKNOWN", reference)).status == 404
    if "untrusted" not in target:
        assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 400
    session.get.assert_not_called()
    await data.async_close()


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.1", "::1", "169.254.169.254", "::ffff:127.0.0.1"])
async def test_actual_resolver_rejects_private_dns(hass, monkeypatch, address):
    monkeypatch.setattr(views.ThreadedResolver, "resolve", AsyncMock(return_value=[{"host": address}]))
    resolver = views.PublicImageResolver()
    try:
        with pytest.raises(OSError, match="Non-public"):
            await resolver.resolve("images.example.invalid", 443)
    finally:
        await resolver.close()


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://127.0.0.1/a", "https://192.168.1.1/a", "https://127.1/a", "https://2130706433/a", "http://cdn.example/image", "https://user:pass@cdn.example/a", "https://cdn.example:8443/a", "https://[invalid/a", "https:///missing-host", "https:", "", " ", "/a\n"])
async def test_proxy_rejects_unsafe_redirect(hass, monkeypatch, location):
    entry, data, proxy, session, response = proxy_fixture(hass, monkeypatch, jpeg())
    response.status = 302
    response.headers = {"Location": location}
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 502
    assert session.get.call_args.kwargs["allow_redirects"] is False
    response.read.assert_not_called()
    await data.async_close()


@pytest.mark.parametrize("kind,key", [("plain", ""), ("png", ""), ("clear-envelope", ""), ("encrypted", ""), ("encrypted", "WRONG"), ("encrypted", "TESTONLY")])
async def test_proxy_published_sdk_decode_contract(hass, monkeypatch, kind, key):
    plain = jpeg()
    body = plain
    if kind == "png":
        with BytesIO() as output, Image.new("RGB", (12, 8)) as picture:
            picture.save(output, "PNG")
            body = plain = output.getvalue()
    if kind in ("clear-envelope", "encrypted"):
        block = plain
        if kind == "encrypted":
            padding = 16 - len(plain) % 16
            cipher = AES.new(b"TESTONLY" + b"\0" * 8, AES.MODE_CBC, b"01234567" + b"\0" * 8)
            block = HIK_ENCRYPTION_HEADER + b"ac837ab144ae11d771426037bf46479e" + cipher.encrypt(plain + bytes([padding]) * padding)
        body = b"mutichhikpic\0\1" + b"".join(pack(">HHII", channel, 1, 4 + len(block), len(block)) + block for channel in (1, 2))
    entry, data, proxy, _, _ = proxy_fixture(hass, monkeypatch, body)
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"enc_key": key}}})
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    result = await proxy.get(None, entry.entry_id, "CAM001", reference)
    if kind == "encrypted" and key != "TESTONLY":
        assert result.status == 400
    else:
        assert result.status == 200 and result.body == plain
        assert result.content_type == ("image/png" if kind == "png" else "image/jpeg")
    await data.async_close()


async def test_registered_siren_publishes_failure_recovery_and_removes_listener(hass, monkeypatch):
    entry, _, data, *_ = entities(hass)
    entity = siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    monkeypatch.setattr(entity, "async_get_last_state", AsyncMock(return_value=None))
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="siren", platform_name="ezviz_cloud", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    try:
        assert hass.states.get(entity.entity_id).state == "off"
        data.async_set_update_error(OSError("synthetic"))
        assert hass.states.get(entity.entity_id).state == "unavailable"
        data.async_set_updated_data(data.data)
        assert hass.states.get(entity.entity_id).state == "off"
        assert data._listeners
    finally:
        await platform.async_reset()
    assert not data._listeners
    await data.async_close()


@pytest.mark.parametrize("kind", ["switch", "light"])
@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("push_serial", ["CAM001", "OTHER"])
async def test_control_display_remains_observed_through_partial_push(hass, monkeypatch, kind, on, push_serial):
    _, _api, data, *_ = entities(hass)
    data.data["CAM001"].update(switches={1: int(not on)}, alarm_light_luminance=10)
    data.data["OTHER"] = deepcopy(data.data["CAM001"])
    if kind == "light":
        desc = light.EzvizLightEntityDescription(key="synthetic", is_on_value=lambda row: row["switches"][1], brightness_value=lambda row: row["alarm_light_luminance"], power_on=lambda *_: True, power_off=lambda *_: True, set_brightness=lambda *_: True)
        entity = light.EzvizLight(data, "CAM001", desc)
    else:
        desc = switch.EzvizSwitchEntityDescription(key="synthetic", value_fn=lambda row: row["switches"][1], method=lambda *_: True)
        entity = switch.EzvizSwitch(data, "CAM001", desc)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    refresh = AsyncMock()
    monkeypatch.setattr(data, "async_request_refresh", refresh)
    method = entity.async_turn_on if on else entity.async_turn_off
    await method(**({"brightness": 205} if kind == "light" and on else {}))
    try:
        assert entity.is_on is (not on)
        data.merge_mqtt_update(push_serial, {"ext": {"image": "https://images.example.invalid/new"}})
        entity._handle_coordinator_update()
        assert entity.is_on is (not on)
        assert data.data["CAM001"]["switches"][1] == int(not on)
        refresh.assert_not_called()
    finally:
        await data.async_close()


@pytest.mark.parametrize("stale", [None, "url", "key", "capture", "shutdown"])
async def test_image_cold_requests_coalesce_and_fence_all_waiters(hass, monkeypatch, stale):
    entry, _, data, _, snapshot, alarm = entities(hass)
    entity = snapshot if stale == "capture" else alarm
    if stale == "capture":
        entity._handle_picture_captured("https://images.example.invalid/capture")
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(_url):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=jpeg())
    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(entity, "_fetch_url", fetch_mock)
    waiters = [asyncio.create_task(entity.async_image()) for _ in range(6)]
    await entered.wait()
    waiters[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiters[0]
    if stale == "url":
        data.data["CAM001"]["last_alarm_pic"] = "https://images.example.invalid/new"
        entity._handle_coordinator_update()
    elif stale == "key":
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"enc_key": "NEW"}}})
        entity._handle_coordinator_update()
    elif stale == "capture":
        entity._handle_picture_captured("https://images.example.invalid/capture")
    elif stale == "shutdown":
        data.begin_shutdown()
    release.set()
    results = await asyncio.gather(*waiters[1:])
    fetch_mock.assert_awaited_once()
    if stale:
        assert results == [None] * 5 and entity._cached_image is None
    else:
        assert all(result == jpeg() for result in results)
        assert await entity.async_image() == jpeg()
        fetch_mock.assert_awaited_once()
    await data.async_close()


async def test_two_lenses_share_concurrent_download(hass, monkeypatch):
    _, _, data, _, _, second = entities(hass, lens_total=2)
    first = EzvizLastMotion(hass, data, "CAM001", {}, lens_index=1, lens_total=2)
    first.hass = hass
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(_url):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=jpeg())
    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(first, "_fetch_url", fetch_mock)
    monkeypatch.setattr(second, "_fetch_url", fetch_mock)
    requests = [asyncio.create_task(entity.async_image()) for entity in (first, second)]
    await entered.wait()
    release.set()
    assert all(await asyncio.gather(*requests))
    fetch_mock.assert_awaited_once()
    await data.async_close()


async def test_push_noops_skip_fanout_and_affected_row_is_detached(hass):
    _, _, data, *_ = entities(hass)
    data.data["OTHER"] = {"nested": {"keep": [1]}}
    old = data.data
    data.async_update_listeners = MagicMock()
    for update in ({}, {"ext": {}}, {"ext": {"unrelated": 1}}):
        data.merge_mqtt_update("CAM001", update)
    assert data.data is old
    data.async_update_listeners.assert_not_called()
    update = {"ext": {"image": "https://images.example.invalid/new"}}
    data.merge_mqtt_update("CAM001", update)
    assert data.data is not old and data.data["CAM001"] is not old["CAM001"]
    assert old["CAM001"]["last_alarm_pic"].endswith("alarm")
    assert data.data["OTHER"] is old["OTHER"]
    data.merge_mqtt_update("CAM001", update)
    data.async_update_listeners.assert_called_once()
    await data.async_close()


@pytest.mark.parametrize("kind", ["switch", "light"])
@pytest.mark.parametrize("failure", [False, OSError("synthetic")])
async def test_failed_controls_do_not_publish_or_refresh(hass, monkeypatch, kind, failure):
    _, _, data, *_ = entities(hass)
    def command(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure
    if kind == "switch":
        desc = switch.EzvizSwitchEntityDescription(key="test", value_fn=lambda row: False, method=command)
        entity = switch.EzvizSwitch(data, "CAM001", desc)
    else:
        desc = light.EzvizLightEntityDescription(key="test", is_on_value=lambda row: False, brightness_value=lambda row: 10, power_on=command, power_off=command)
        entity = light.EzvizLight(data, "CAM001", desc)
    refresh = AsyncMock()
    monkeypatch.setattr(data, "async_request_refresh", refresh)
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    assert not entity.is_on
    refresh.assert_not_called()
    await data.async_close()


async def test_proxy_owns_actual_connector_until_ha_close(hass):
    proxy = views.ImageProxyView(hass)
    assert not proxy.session.closed
    await hass.async_stop()
    assert proxy.session.closed


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["https://cdn.example/image", "../image", "//cdn.example/image"])
async def test_proxy_follows_validated_cdn_redirect_and_decodes(hass, monkeypatch, status, location, encrypted):
    plain = jpeg()
    block = plain
    if encrypted:
        padding = 16 - len(plain) % 16
        cipher = AES.new(b"TESTONLY" + b"\0" * 8, AES.MODE_CBC, b"01234567" + b"\0" * 8)
        block = HIK_ENCRYPTION_HEADER + b"ac837ab144ae11d771426037bf46479e" + cipher.encrypt(plain + bytes([padding]) * padding)
    envelope = b"mutichhikpic\0\1" + b"".join(pack(">HHII", channel, 1, 4 + len(block), len(block)) + block for channel in (1, 2))
    entry, data, proxy, session, final = proxy_fixture(hass, monkeypatch, envelope)
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {"enc_key": "TESTONLY" if encrypted else ""}}})
    redirect = SimpleNamespace(status=status, url="https://images.example.invalid/actual/dir/image", headers={"Location": location}, release=MagicMock())
    session.get.side_effect = [redirect, final]
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    result = await proxy.get(None, entry.entry_id, "CAM001", reference)
    assert result.status == 200 and result.body == plain
    assert result.content_type == "image/jpeg"
    expected = "https://images.example.invalid/actual/image" if location == "../image" else "https://cdn.example/image"
    assert session.get.call_args.args == (expected,)
    for call in session.get.call_args_list:
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"] == {"User-Agent": "HomeAssistant/ezviz_cloud", "Accept": "*/*"}
    redirect.release.assert_called_once()
    final.release.assert_called_once()
    await data.async_close()


async def test_proxy_redirect_limit_releases_every_response(hass, monkeypatch):
    entry, data, proxy, session, response = proxy_fixture(hass, monkeypatch, jpeg())
    response.status = 302
    response.headers = {"Location": "/next"}
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    result = await proxy.get(None, entry.entry_id, "CAM001", reference)
    assert result.status == 502
    assert session.get.call_count == views._MAX_IMAGE_REDIRECTS + 1
    assert response.release.call_count == session.get.call_count
    response.read.assert_not_called()
    await data.async_close()


async def test_proxy_missing_redirect_location(hass, monkeypatch):
    entry, data, proxy, session, response = proxy_fixture(hass, monkeypatch, jpeg())
    response.status = 302
    response.headers = {}
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 502
    assert session.get.call_count == 1
    response.release.assert_called_once()
    await data.async_close()


async def test_proxy_rebound_redirect_uses_public_resolver(hass, monkeypatch):
    entry, data, proxy, session, response = proxy_fixture(hass, monkeypatch, jpeg())
    response.status = 302
    response.headers = {"Location": "https://cdn.example/image"}
    resolver = views.PublicImageResolver()
    monkeypatch.setattr(views.ThreadedResolver, "resolve", AsyncMock(return_value=[{"host": "127.0.0.1"}]))

    async def connect(url, **kwargs):
        if url == "https://cdn.example/image":
            try:
                await resolver.resolve("cdn.example", 443)
            except OSError as err:
                raise views.ClientError("blocked") from err
        return response

    session.get.side_effect = connect
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    try:
        assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 400
        response.read.assert_not_called()
        response.release.assert_called_once()
    finally:
        await resolver.close()
        await data.async_close()


async def test_proxy_deadline_spans_redirect_and_body(hass, monkeypatch):
    entry, data, proxy, session, response = proxy_fixture(hass, monkeypatch, jpeg())
    real_timeout = asyncio.timeout
    deadlines = []

    def short_timeout(seconds):
        deadlines.append(seconds)
        return real_timeout(0.01)

    monkeypatch.setattr(views, "timeout", short_timeout)
    redirect = SimpleNamespace(status=302, url=response.url, headers={"Location": "/next"}, release=MagicMock())
    session.get.side_effect = [redirect, response]

    async def stalled_body():
        await asyncio.Event().wait()

    response.read.side_effect = stalled_body
    reference = data.register_image("CAM001", "https://images.example.invalid/image")
    assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 400
    assert deadlines == [30]
    redirect.release.assert_called_once()
    response.release.assert_called_once()
    await data.async_close()
