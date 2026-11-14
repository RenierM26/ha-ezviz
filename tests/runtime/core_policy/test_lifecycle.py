"""Timer, RTSP and account-scoped push lifecycle regressions without device IO."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.ezviz_cloud import mqtt, siren
from custom_components.ezviz_cloud.const import DOMAIN
import pytest

from homeassistant.components.camera import Camera
from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.stream import Stream
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr

from tests.runtime.test_hardening import entities, entry_for


@pytest.mark.parametrize("restored", [False, True])
async def test_siren_retrigger_and_remove_reject_queued_old_timers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, restored: bool
) -> None:
    """Even cancelled callbacks queued before retrigger/removal cannot write state."""
    _entry, api, data, *_ = entities(hass)
    api.sound_alarm = MagicMock(return_value=True)
    callbacks = []
    cancellations = []

    def schedule(*args: object) -> MagicMock:
        callbacks.append(args[2])
        cancel = MagicMock()
        cancellations.append(cancel)
        return cancel

    monkeypatch.setattr(siren.evt, "async_call_later", schedule)
    entity = siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(return_value=State("siren.synthetic", "on") if restored else None),
    )
    await entity.async_added_to_hass()
    if not restored:
        await entity.async_turn_on()
    assert len(callbacks) == 1
    await entity.async_turn_on()
    cancellations[0].assert_called_once()
    callbacks[0](datetime.now(UTC))
    assert entity.is_on
    callbacks[1](datetime.now(UTC))
    assert not entity.is_on
    await entity.async_turn_on()
    await entity.async_turn_off()
    callbacks[2](datetime.now(UTC))
    assert not entity.is_on
    await entity.async_turn_on()
    await entity.async_will_remove_from_hass()
    cancellations[-1].assert_called_once()
    entity.async_write_ha_state.reset_mock()
    callbacks[-1](datetime.now(UTC))
    await entity.async_turn_on()
    entity.async_write_ha_state.assert_not_called()
    assert len(callbacks) == 4
    await data.async_close()


@pytest.mark.parametrize("verification", [False, True])
async def test_committed_rtsp_selection_updates_existing_stream(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, verification: bool
) -> None:
    """Committed keys/options update snapshots and streams, not unselected secrets."""
    entry, _api, data, cam, *_ = entities(hass)
    opts = {
        "username": "admin",
        "password": "sticker",
        "enc_key": "key",
        "rtsp_uses_verification_code": verification,
        "ffmpeg_arguments": "new/path",
    }
    await cam.async_added_to_hass()
    cam.stream = MagicMock(dynamic_stream_settings=DynamicStreamSettings())
    cam.stream.stop = AsyncMock()
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": opts}})
    await hass.async_block_till_done()
    selected = "sticker" if verification else "key"
    assert (
        await cam.stream_source() == f"rtsp://admin:{selected}@192.0.2.1:554/new/path"
    )
    cam.stream.update_source.assert_called_once_with(await cam.stream_source())
    cam.stream.update_source.reset_mock()
    data.credentials.commit("CAM001", "new-key")
    await hass.async_block_till_done()
    assert cam.stream.update_source.call_count == (0 if verification else 1)
    opts = dict(entry.options["cameras"]["CAM001"])
    opts.update(username="u@ser", password="p:/@ss", enc_key="p:/@ss")
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": opts}})
    await hass.async_block_till_done()
    assert "u%40ser:p%3A%2F%40ss@" in await cam.stream_source()
    data.data["CAM001"]["local_ip"] = "192.0.2.2"
    cam._handle_coordinator_update()
    assert "192.0.2.2" in cam.stream.update_source.call_args.args[0]
    cam.stream.update_source.reset_mock()
    await cam.async_will_remove_from_hass()
    await cam._async_options_updated(hass, entry)
    cam._handle_coordinator_update()
    cam.stream.update_source.assert_not_called()
    cam.stream.stop.assert_awaited_once()
    assert await cam.async_create_stream() is None
    await data.async_close()


async def test_stream_creation_serializes_with_commit_and_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream created across an await gets the latest key and is stopped on unload."""
    _entry, _api, data, cam, *_ = entities(hass)
    started, finish = asyncio.Event(), asyncio.Event()
    stream = MagicMock(
        source="rtsp://old", dynamic_stream_settings=DynamicStreamSettings()
    )
    stream.stop = AsyncMock()

    async def create(_self: Camera) -> MagicMock:
        started.set()
        await finish.wait()
        cam.stream = stream
        return stream

    monkeypatch.setattr(Camera, "async_create_stream", create)
    creation = asyncio.create_task(cam.async_create_stream())
    await started.wait()
    data.credentials.commit("CAM001", "new-key")
    finish.set()
    assert await creation is stream
    stream.update_source.assert_called_with(
        "rtsp://admin:new-key@192.0.2.1:554/Streaming/Channels/102"
    )
    cam.stream = None
    finish.clear()
    started.clear()
    creation = asyncio.create_task(cam.async_create_stream())
    await started.wait()
    removal = asyncio.create_task(cam.async_will_remove_from_hass())
    await asyncio.sleep(0)
    assert not removal.done()
    finish.set()
    await creation
    await removal
    stream.stop.assert_awaited_once()
    assert await cam.async_create_stream() is None
    await data.async_close()


@pytest.mark.parametrize("registered", [False, True])
async def test_push_device_resolution_is_entry_scoped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    """Push identity uses the owning account; unknown devices keep a null ID."""
    entry, api, data, *_ = entities(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "CAM001")}
    )
    other = entry_for(hass)
    other_device = registry.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(DOMAIN, "OTHER")}
    )
    assert device.id != other_device.id
    lookup = registry.async_get_device_by_identifier
    spy = MagicMock(wraps=lookup)
    monkeypatch.setattr(registry, "async_get_device_by_identifier", spy)
    monkeypatch.setattr(
        registry,
        "async_get_device",
        MagicMock(side_effect=AssertionError("deprecated lookup")),
    )
    handler = mqtt.EzvizMqttHandler(hass, api, entry.entry_id)
    handler._coordinator = data
    data.merge_mqtt_update = MagicMock()
    events = []
    hass.bus.async_listen("ezviz_push_event", lambda event: events.append(event.data))
    serial = "CAM001" if registered else "OTHER"
    payload = {"ext": {"device_serial": serial}, "synthetic": True}
    handler._on_message(payload)
    await hass.async_block_till_done()
    spy.assert_called_once_with((DOMAIN, serial), entry.entry_id)
    assert events[0]["device_id"] == (device.id if registered else None)
    assert "device_id" not in payload
    assert events[0]["synthetic"]
    handler._stopping.set()
    handler._on_message(payload)
    await hass.async_block_till_done()
    assert len(events) == 1
    await data.async_close()


async def test_preloaded_stream_worker_stops_on_entity_removal(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use HA's real source restart and stop implementation, including preload."""
    _entry, _api, data, cam, *_ = entities(hass)
    stream = Stream(
        hass,
        await cam.stream_source(),
        {},
        STREAM_SETTINGS_NON_LL_HLS,
        DynamicStreamSettings(preload_stream=True),
    )
    cam.stream = stream
    preferences = stream.dynamic_stream_settings
    worker = MagicMock()
    stream._thread = worker
    data.credentials.commit("CAM001", "rotated")
    cam._handle_coordinator_update()
    assert "rotated@" in stream.source
    assert stream._thread_quit.is_set()
    assert stream._fast_restart_once
    await cam.async_will_remove_from_hass()
    worker.join.assert_called_once()
    assert stream._thread is None
    assert not stream.dynamic_stream_settings.preload_stream
    assert preferences.preload_stream
    await data.async_close()
