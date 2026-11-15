"""Transport normalization and compound light command regression tests."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError, ClientSession, TCPConnector
import pytest
from yarl import URL

from custom_components.ezviz_cloud import light, views
from homeassistant.exceptions import HomeAssistantError

from .test_hardening import entities
from .test_integration_audit import jpeg, proxy_fixture

UNICODE_PRIVATE_HOSTS = (
    "127\u30020\u30020\u30021",
    "192\u3002168\u30021\u30021",
    "127\uff0e0\uff0e0\uff0e1",
    "192\uff0e168\uff0e1\uff0e1",
    "127\uff610\uff610\uff611",
    "192\uff61168\uff611\uff611",
    "\uff11\uff12\uff17.\uff10.\uff10.\uff11",
    "\uff11\uff19\uff12.\uff11\uff16\uff18.\uff11.\uff11",
)


@pytest.mark.parametrize("host", UNICODE_PRIVATE_HOSTS)
async def test_transport_normalization_bypasses_dns_before_socket(hass, monkeypatch, host):
    """Prove the real transport bypasses resolvers for these normalized literals."""
    resolver = views.PublicImageResolver()
    lookup = AsyncMock(side_effect=AssertionError("Literal IP must bypass DNS"))
    monkeypatch.setattr(resolver, "resolve", lookup)
    connector = TCPConnector(resolver=resolver)
    connect = AsyncMock(side_effect=ClientError("Intercepted before socket creation"))
    monkeypatch.setattr(connector, "_wrap_create_connection", connect)
    try:
        async with ClientSession(connector=connector) as session:
            with pytest.raises(ClientError, match="Intercepted"):
                await session.get(f"https://{host}/synthetic")
        connect.assert_awaited_once()
        assert connect.call_args.kwargs["req"].url.raw_host == URL(f"https://{host}").raw_host
        lookup.assert_not_called()
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "host",
    (
        *UNICODE_PRIVATE_HOSTS,
        "\uff11\uff12\uff17\u3002\uff11",
        "\uff12\uff11\uff13\uff10\uff17\uff10\uff16\uff14\uff13\uff13",
    ),
)
@pytest.mark.parametrize("redirect", [False, True])
async def test_proxy_rejects_normalized_private_hosts_before_transport(
    hass, monkeypatch, host, redirect
):
    entry, _, data, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    proxy = views.ImageProxyView(hass)
    source = "https://images.example.invalid/image"
    target = f"https://{host}/synthetic?signature=a%2Fb%2B%3D"
    response = SimpleNamespace(
        status=302, url=URL(source), headers={"Location": target}, release=MagicMock()
    )
    real_get = proxy.session.get

    async def fetch(url, **kwargs):
        if redirect and url == source:
            return response
        return await real_get(url, **kwargs)

    monkeypatch.setattr(proxy.session, "get", fetch)
    connector = proxy.session.connector
    connect = AsyncMock(side_effect=ClientError("Intercepted before socket creation"))
    lookup = AsyncMock(side_effect=AssertionError("Private target reached DNS"))
    monkeypatch.setattr(connector, "_wrap_create_connection", connect)
    monkeypatch.setattr(connector._resolver, "resolve", lookup)
    reference = data.register_image("CAM001", source if redirect else target)
    try:
        result = await proxy.get(None, entry.entry_id, "CAM001", reference)
        assert result.status == (502 if redirect else 400)
        connect.assert_not_called()
        lookup.assert_not_called()
        if redirect:
            response.release.assert_called_once()
    finally:
        await proxy.session.close()
        await data.async_close()


@pytest.mark.parametrize("redirect", [False, True])
async def test_normalized_public_host_keeps_signed_request_string(hass, monkeypatch, redirect):
    entry, data, proxy, session, final = proxy_fixture(hass, monkeypatch, jpeg())
    target = "https://cdn\u3002example/image?signature=a%2Fb%2B%3D&x=1&x=2"
    assert views._public_image_url(target)
    source = "https://images.example.invalid/image"
    if redirect:
        response = SimpleNamespace(
            status=302, url=URL(source), headers={"Location": target}, release=MagicMock()
        )
        session.get.side_effect = [response, final]
    reference = data.register_image("CAM001", source if redirect else target)
    try:
        assert (await proxy.get(None, entry.entry_id, "CAM001", reference)).status == 200
        assert session.get.call_args.args == (target,)
    finally:
        await data.async_close()


@pytest.mark.parametrize(
    "power_result",
    [
        True,
        False,
        OSError("synthetic power failure"),
        TimeoutError("synthetic"),
        HomeAssistantError("synthetic"),
    ],
)
async def test_light_batches_writes_before_real_debounced_refresh(hass, monkeypatch, power_result):
    """Use real Core refresh/debouncer and observe actual command side effects."""
    _, _, data, *_ = entities(hass)
    physical = deepcopy(data.data)
    physical["CAM001"].update(switches={1: 0}, alarm_light_luminance=10)
    data.data = deepcopy(physical)
    calls = []

    def brightness(_client, _serial, percent):
        calls.append("brightness")
        physical["CAM001"]["alarm_light_luminance"] = percent
        return True

    def power(*_):
        calls.append("power")
        if isinstance(power_result, Exception):
            raise power_result
        if power_result:
            physical["CAM001"]["switches"][1] = 1
        return power_result

    entity = light.EzvizLight(
        data,
        "CAM001",
        light.EzvizLightEntityDescription(
            key="test",
            is_on_value=lambda row: row["switches"][1],
            brightness_value=lambda row: row["alarm_light_luminance"],
            set_brightness=brightness,
            power_on=power,
            power_off=power,
        ),
    )
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    remove_listener = data.async_add_listener(entity._handle_coordinator_update)
    entered, release = asyncio.Event(), asyncio.Event()

    async def poll():
        calls.append("poll")
        observed = deepcopy(physical)
        entered.set()
        await release.wait()
        return observed

    # Only replace the cloud polling boundary, not Core refresh or its debouncer.
    monkeypatch.setattr(data, "_async_update_data", poll)
    task = asyncio.create_task(entity.async_turn_on(brightness=205))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        # Power has already been attempted even while the observation is blocked.
        assert calls == ["brightness", "power", "poll"]
        assert not entity.is_on and entity.brightness == 26
        release.set()
        if power_result is True:
            await task
        else:
            with pytest.raises(HomeAssistantError):
                await task
        assert entity.is_on is (power_result is True)
        assert physical["CAM001"]["alarm_light_luminance"] == 80
        assert entity.brightness == 204
        assert data.data == physical
        assert calls == ["brightness", "power", "poll"]
        # No second observation was queued for after the cooldown.
        assert not data._debounced_refresh._execute_at_end_of_timer
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        remove_listener()
        await data.async_close()


@pytest.mark.parametrize("failure", [False, OSError("synthetic brightness failure")])
async def test_light_rejected_first_write_never_powers_or_polls(hass, monkeypatch, failure):
    _, _, data, *_ = entities(hass)

    def brightness(*_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    power = MagicMock(return_value=True)
    poll = AsyncMock()
    monkeypatch.setattr(data, "_async_update_data", poll)
    entity = light.EzvizLight(
        data,
        "CAM001",
        light.EzvizLightEntityDescription(
            key="test",
            is_on_value=lambda row: False,
            brightness_value=lambda row: 10,
            set_brightness=brightness,
            power_on=power,
            power_off=power,
        ),
    )
    try:
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on(brightness=205)
        power.assert_not_called()
        poll.assert_not_called()
        assert not entity.is_on and entity.brightness == 26
    finally:
        await data.async_close()
