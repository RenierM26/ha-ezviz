"""Malformed/stale media identifiers produce HA errors, not raw exceptions."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pyezvizapi.exceptions import PyEzvizError
import pytest
from test_hardening import entities

from custom_components.ezviz_cloud import media_source as media
from homeassistant.components.media_source import Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError


@pytest.mark.parametrize(
    "identifier",
    [
        "CAM",
        "CAM|entry",
        "CAM||serial",
        "CAM|entry|",
        "CAM|../entry|serial",
        "CAM|entry|../serial",
        "CAM|entry|serial|",
        "CAM|entry|serial|0",
        "CAM|entry|serial|-1",
        "CAM|entry|serial|201",
        "CAM|entry|serial|1000",
        "CAM|entry|serial|1.5",
        "CAM|entry|serial|NaN",
        "CAM|entry|serial| 2",
        "CAM|entry|serial|٢",
        "CAM|entry|serial|10|extra",
        "UNKNOWN|entry|serial",
        "CAM|" + "x" * media.MAX_IDENTIFIER_LENGTH,
    ],
)
async def test_invalid_camera_identifiers_are_domain_errors(hass, identifier):
    source = media.EzvizMediaSource(hass)
    with pytest.raises(Unresolvable, match="Invalid EZVIZ camera"):
        await source.async_browse_media(SimpleNamespace(identifier=identifier))


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "EIMG",
        "EIMG|entry",
        "EIMG|entry|serial",
        "EIMG||serial|YWJj",
        "EIMG|entry||YWJj",
        "EIMG|entry|serial|!!!!",
        "EIMG|entry|serial|_w==",
        "EIMG|entry|serial|YWJj|extra",
        "IMG|YWJj",
        "IMG|YWJj|text/html",
        "IMG|YWJj|image/jpeg|extra",
        "UNKNOWN|YWJj|image/jpeg",
        "EIMG|entry|serial|☃",
        "IMG|" + "x" * media.MAX_IDENTIFIER_LENGTH + "|image/jpeg",
    ],
)
async def test_invalid_image_identifiers_are_domain_errors(hass, identifier):
    source = media.EzvizMediaSource(hass)
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(SimpleNamespace(identifier=identifier))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "file:///etc/passwd",
        "ftp://example.invalid/a",
        "https:///missing",
        "https://user:secret@example.invalid/a",
        "https://example.invalid/a#fragment",
        "https://example.invalid:bad/a",
        "https://example.invalid:99999/a",
        "https://example.invalid/line\nbreak",
    ],
)
async def test_invalid_stored_image_urls_are_domain_errors(hass, url):
    source = media.EzvizMediaSource(hass)
    for identifier in [
        f"IMG|{media._b64e(url)}|image/jpeg",
        f"EIMG|entry|serial|{media._b64e(url)}",
    ]:
        with pytest.raises(Unresolvable):
            await source.async_resolve_media(SimpleNamespace(identifier=identifier))


@pytest.mark.parametrize("legacy", [False, True])
async def test_valid_image_resolution_preserves_signed_urls(hass, legacy):
    source = media.EzvizMediaSource(hass)
    url = "https://images.example.invalid/a?sig=synthetic&expires=123"
    identifier = (
        f"IMG|{media._b64e(url)}|image/png" if legacy else f"EIMG|entry|serial|{media._b64e(url)}"
    )
    result = await source.async_resolve_media(SimpleNamespace(identifier=identifier))
    if legacy:
        assert result.url == url
        assert result.mime_type == "image/png"
    else:
        assert result.url == media.async_generate_image_proxy_url("entry", "serial", url)
        assert result.mime_type == "image/jpeg"


@pytest.mark.parametrize("limit", [None, 1, 200])
async def test_stale_camera_identifier_is_a_safe_empty_directory(hass, limit):
    source = media.EzvizMediaSource(hass)
    result = await source.async_browse_media(
        SimpleNamespace(
            identifier="CAM|missing|serial" + (f"|{limit}" if limit is not None else "")
        )
    )
    assert result.children == []
    assert not result.can_expand and not result.can_play


async def test_loaded_alarm_browse_and_root_share_valid_normalized_identifiers(hass):
    entry, api, data, _, _, _ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=data)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    api.get_alarminfo = MagicMock(
        return_value={
            "alarms": [
                {
                    "deviceSerial": "CAM001",
                    "picUrl": "https://images.example.invalid/one?sig=synthetic",
                    "alarmStartTimeStr": "today",
                    "sampleName": "Motion",
                },
                {"picUrl": "https://images.example.invalid/two"},
                {"deviceSerial": "CAM001", "picUrl": "file:///invalid"},
                None,
            ]
        }
    )
    source = await media.async_get_media_source(hass)
    root = await source.async_browse_media(SimpleNamespace(identifier=""))
    assert len(root.children) == 1
    assert root.children[0].identifier == f"CAM|{entry.entry_id}|CAM001"
    result = await source.async_browse_media(
        SimpleNamespace(identifier=root.children[0].identifier)
    )
    api.get_alarminfo.assert_called_once_with("CAM001", media.DEFAULT_LIMIT)
    assert len(result.children) == 2
    assert all(
        item.identifier.startswith(f"EIMG|{entry.entry_id}|CAM001|") for item in result.children
    )
    for item in result.children:
        assert (
            await source.async_resolve_media(SimpleNamespace(identifier=item.identifier))
        ).mime_type == "image/jpeg"
    await data.async_close()


@pytest.mark.parametrize(
    "response",
    [None, [], {"alarms": None}, {"alarms": "invalid"}, {"alarms": [False, 1, "invalid"]}],
)
async def test_malformed_alarm_payloads_are_normalized_to_empty(hass, response):
    _, api, data, _, _, _ = entities(hass)
    api.get_alarminfo = MagicMock(return_value=response)
    assert (
        await media.EzvizMediaSource(hass)._async_get_alarms(data, serial="CAM001", limit=1) == []
    )
    await data.async_close()


@pytest.mark.parametrize("error", [PyEzvizError, OSError, TimeoutError, HomeAssistantError])
async def test_alarm_errors_are_recoverable_domain_errors(hass, error):
    _, api, data, _, _, _ = entities(hass)
    api.get_alarminfo = MagicMock(side_effect=error("synthetic"))
    source = media.EzvizMediaSource(hass)
    with pytest.raises(Unresolvable, match="unavailable"):
        await source._async_get_alarms(data, serial="CAM001", limit=1)
    api.get_alarminfo.side_effect = None
    api.get_alarminfo.return_value = {"alarms": [{"picUrl": "one"}, {"picUrl": "two"}]}
    assert await source._async_get_alarms(data, serial="CAM001", limit=1) == [{"picUrl": "one"}]
    await data.async_close()
