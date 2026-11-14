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
from homeassistant.helpers.entity_platform import EntityPlatform

from .test_hardening import entities


def jpeg():
    with BytesIO() as output, Image.new("RGB", (12, 8)) as picture:
        picture.save(output, "JPEG")
        return output.getvalue()


def proxy_fixture(hass, monkeypatch, body):
    entry, _api, data, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    response = SimpleNamespace(status=200, read=AsyncMock(return_value=body), release=MagicMock())
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(views, "ClientSession", lambda **kwargs: session)
    return entry, data, views.ImageProxyView(hass), session, response


@pytest.mark.parametrize("target", ["http://127.0.0.1/a", "https://127.0.0.1/a", "https://192.168.1.1/a", "https://[::1]/a", "https://untrusted.example/a"])
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


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://other.example/image"])
async def test_proxy_does_not_follow_any_redirect(hass, monkeypatch, location):
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
async def test_control_awaits_observation_through_partial_push(hass, monkeypatch, kind, on, push_serial):
    _, _api, data, *_ = entities(hass)
    data.data["CAM001"].update(switches={1: 0}, alarm_light_luminance=10)
    data.data["OTHER"] = deepcopy(data.data["CAM001"])
    if kind == "light":
        desc = light.EzvizLightEntityDescription(key="synthetic", is_on_value=lambda row: row["switches"][1], brightness_value=lambda row: row["alarm_light_luminance"], power_on=lambda *_: True, power_off=lambda *_: True, set_brightness=lambda *_: True)
        entity = light.EzvizLight(data, "CAM001", desc)
    else:
        desc = switch.EzvizSwitchEntityDescription(key="synthetic", value_fn=lambda row: row["switches"][1], method=lambda *_: True)
        entity = switch.EzvizSwitch(data, "CAM001", desc)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()
    async def refresh():
        entered.set()
        await release.wait()
        data.data["CAM001"]["switches"][1] = int(on)
        data.data["CAM001"]["alarm_light_luminance"] = 80
        entity._handle_coordinator_update()
    monkeypatch.setattr(data, "async_request_refresh", refresh)
    method = entity.async_turn_on if on else entity.async_turn_off
    task = asyncio.create_task(method(**({"brightness": 205} if kind == "light" and on else {})))
    await entered.wait()
    data.merge_mqtt_update(push_serial, {"ext": {"image": "https://images.example.invalid/new"}})
    entity._handle_coordinator_update()
    assert not entity.is_on
    release.set()
    await task
    assert entity.is_on is on
    await data.async_close()
