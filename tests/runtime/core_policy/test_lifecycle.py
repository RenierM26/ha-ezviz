"""Timer, RTSP and account-scoped push lifecycle regressions without device IO."""

import asyncio
from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.metadata import version
import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.ezviz_cloud import mqtt, siren
from custom_components.ezviz_cloud.const import DOMAIN
import pytest

from homeassistant.components.camera import Camera
from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.stream import Stream
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

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
async def test_credential_commit_updates_existing_stream(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, verification: bool
) -> None:
    """Credential actions update streams without entry listeners or account reloads."""
    entry, _api, data, cam, *_ = entities(hass)
    opts = {
        "username": "u@ser",
        "password": "p:/@ss",
        "enc_key": "key",
        "rtsp_uses_verification_code": verification,
        "ffmpeg_arguments": "new/path",
    }
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": opts}})
    await cam.async_added_to_hass()
    assert not entry.update_listeners
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    cam.stream = MagicMock(dynamic_stream_settings=DynamicStreamSettings())
    cam.stream.stop = AsyncMock()
    selected = "p%3A%2F%40ss" if verification else "key"
    assert (
        await cam.stream_source() == f"rtsp://u%40ser:{selected}@192.0.2.1:554/new/path"
    )
    cam.stream.update_source.assert_not_called()
    data.credentials.commit("CAM001", "new-key")
    await hass.async_block_till_done()
    assert cam.stream.update_source.call_count == (0 if verification else 1)
    reload_entry.assert_not_awaited()
    data.data["CAM001"]["local_ip"] = "192.0.2.2"
    cam._handle_coordinator_update()
    assert "192.0.2.2" in cam.stream.update_source.call_args.args[0]
    cam.stream.update_source.reset_mock()
    stream = cam.stream
    await cam.async_will_remove_from_hass()
    cam._refresh_stream_source()
    cam._handle_coordinator_update()
    stream.update_source.assert_not_called()
    stream.stop.assert_awaited_once()
    assert cam.stream is None
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
    assert await creation is None
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


@pytest.mark.parametrize("kind", ["camera", "siren"])
@pytest.mark.parametrize("restored", [False, True])
async def test_registry_rename_keeps_entity_operational(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, kind: str, restored: bool
) -> None:
    """Real registry rename re-adds the same object with usable state and stream."""
    entry, api, data, cam, *_ = entities(hass)
    api.sound_alarm = MagicMock(return_value=True)
    data.data["CAM001"]["alarm_notify"] = 0
    cam.entity_id = None
    del cam.async_write_ha_state
    entity = (
        cam
        if kind == "camera"
        else siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    )
    monkeypatch.setattr(
        siren.EzvizSirenEntity,
        "async_get_last_state",
        AsyncMock(return_value=State("siren.synthetic", "on") if restored else None),
    )
    created = []
    preferences = DynamicStreamSettings(preload_stream=True)

    def create(*args: object, **kwargs: object) -> Stream:
        stream = Stream(hass, str(args[1]), {}, STREAM_SETTINGS_NON_LL_HLS, preferences)
        created.append(stream)
        return stream

    camera_module = import_module(Camera.__module__)
    monkeypatch.setattr(camera_module, "create_stream", create)
    monkeypatch.setattr(
        camera_module,
        "get_dynamic_camera_stream_settings",
        AsyncMock(return_value=preferences),
    )
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain=kind,
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    try:
        if kind == "camera":
            original = await cam.async_create_stream()
        previous = entity.entity_id
        renamed = f"{kind}.renamed"
        er.async_get(hass).async_update_entity(previous, new_entity_id=renamed)
        await hass.async_block_till_done()
        assert previous not in platform.entities
        assert platform.entities[renamed] is entity
        assert not entity._removed
        if kind == "camera":
            stream = await cam.async_create_stream()
            assert stream is not None
            assert stream.dynamic_stream_settings.preload_stream
            assert preferences.preload_stream
            assert len(created) == 2
            assert stream is not original
            data.data["CAM001"]["local_ip"] = "192.0.2.2"
            data.async_set_updated_data(data.data)
            assert "192.0.2.2" in stream.source
        else:
            await entity.async_turn_on()
            assert hass.states.get(renamed).state == "on"
            assert entity._delay_listener is not None
            entity._cancel_off_delay()
            entity.off_delay_listener(datetime.now(UTC))
            assert hass.states.get(renamed).state == "off"
            await entity.async_turn_off()
            assert api.sound_alarm.call_count == 2
    finally:
        await platform.async_reset()
        await data.async_close()


@pytest.mark.parametrize("turn_on", [False, True])
async def test_siren_old_command_cannot_write_after_readd(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, turn_on: bool
) -> None:
    """Resetting the removal flag must not admit results from the prior lifetime."""
    _, api, data, *_ = entities(hass)
    entity = siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    api.sound_alarm = MagicMock()
    monkeypatch.setattr(entity, "async_get_last_state", AsyncMock(return_value=None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*args: object) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(data, "async_execute", execute)
    await entity.async_added_to_hass()
    command = entity.async_turn_on if turn_on else entity.async_turn_off
    pending = asyncio.create_task(command())
    await entered.wait()
    await entity.async_will_remove_from_hass()
    await entity.async_added_to_hass()
    release.set()
    await pending
    entity.async_write_ha_state.assert_not_called()
    assert entity._delay_listener is None
    await entity.async_will_remove_from_hass()
    await data.async_close()


@pytest.mark.parametrize("elapsed", [20, 61])
async def test_active_siren_rename_preserves_remaining_expiry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, elapsed: int
) -> None:
    """A real rename has no restored record at the new ID; keep the old deadline."""
    entry, api, data, *_ = entities(hass)
    api.sound_alarm = MagicMock(return_value=True)
    entity = siren.EzvizSirenEntity(data, "CAM001", siren.SIREN_ENTITY_TYPE)
    scheduled = []
    cancels = []
    clock = [1000.0]
    monkeypatch.setattr(siren, "monotonic", lambda: clock[0], raising=False)

    def schedule(*args: object) -> MagicMock:
        scheduled.append((args[1], args[2]))
        cancel = MagicMock()
        cancels.append(cancel)
        return cancel

    monkeypatch.setattr(siren.evt, "async_call_later", schedule)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="siren",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    try:
        assert not entity.is_on
        assert not scheduled
        await entity.async_turn_on()
        assert entity.is_on
        assert len(scheduled) == 1
        clock[0] += elapsed
        er.async_get(hass).async_update_entity(
            entity.entity_id, new_entity_id="siren.renamed_active"
        )
        await hass.async_block_till_done()
        assert platform.entities["siren.renamed_active"] is entity
        if version("homeassistant") == "2026.10.4":
            assert await entity.async_get_last_state() is None
        if elapsed >= 60:
            assert not entity.is_on
            assert entity._delay_listener is None
            assert len(scheduled) == 1
            assert hass.states.get(entity.entity_id).state == "off"
            return
        assert entity.is_on
        assert entity._delay_listener is not None
        assert len(scheduled) == 2
        assert scheduled[-1][0] == 40
        cancels[0].assert_called_once()
        scheduled[0][1](datetime.now(UTC))
        assert entity.is_on
        clock[0] += 10
        er.async_get(hass).async_update_entity(
            entity.entity_id, new_entity_id="siren.renamed_again"
        )
        await hass.async_block_till_done()
        assert scheduled[-1][0] == 30
        clock[0] += 30
        scheduled[-1][1](datetime.now(UTC))
        assert not entity.is_on
        assert hass.states.get(entity.entity_id).state == "off"
    finally:
        await platform.async_reset()
        await data.async_close()
