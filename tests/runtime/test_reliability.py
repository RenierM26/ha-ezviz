"""Regression controls for cancellation, concurrent options and cleanup outcomes."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.utils import return_password_hash
import pytest
import test_config_flow as controls
from test_hardening import entities

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import runtime, text
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import EVENT_HOMEASSISTANT_STOP, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

integration = controls.integration


@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
async def test_options_commit_latest_values_and_require_review_for_conflicts(
    hass, monkeypatch, mfa, conflict
):
    entry, client, _, result = await controls.start_camera(hass, monkeypatch)
    manager = hass.config_entries.options
    if mfa:
        client.get_cam_key.side_effect = controls.EzvizAuthVerificationCode()
        result = await manager.async_configure(
            result["flow_id"], {**controls.CAMERA, "enc_key": controls.DEFAULT_FETCH_MY_KEY}
        )
        assert result["step_id"] == "camera_edit_2fa"

    async def resolve(_handler, _payload):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                "timeout": 87,
                "cameras": {
                    **entry.options["cameras"],
                    "OTHER": {"password": "concurrent-new"},
                    "CAM": {
                        "future_field": True,
                        **({"enc_key": "concurrent-key"} if conflict else {}),
                    },
                },
            },
        )
        return {**controls.CAMERA, "enc_key": "would-overwrite"}

    monkeypatch.setattr(controls.flow.EzvizOptionsFlowHandler, "_test_rtsp_credentials", resolve)
    result = await manager.async_configure(result["flow_id"], {} if mfa else controls.CAMERA)
    assert entry.options["timeout"] == 87
    assert entry.options["cameras"]["OTHER"]["password"] == "concurrent-new"
    if conflict:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "camera_edit"
        assert result["errors"] == {"base": "options_changed"}
        assert result["data_schema"]({})["enc_key"] == "concurrent-key"
        assert entry.options["cameras"]["CAM"]["enc_key"] == "concurrent-key"
        monkeypatch.setattr(
            controls.flow.EzvizOptionsFlowHandler,
            "_test_rtsp_credentials",
            AsyncMock(return_value={**controls.CAMERA, "enc_key": "concurrent-key"}),
        )
        result = await manager.async_configure(result["flow_id"], result["data_schema"]({}))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cameras"]["CAM"]["future_field"] is True
    assert entry.options["cameras"]["CAM"]["enc_key"] == (
        "concurrent-key" if conflict else "would-overwrite"
    )


@pytest.mark.parametrize("operation", ["change", "fetch"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_credential_operation_commits_across_cancellation_and_unload(hass, operation, cancel):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={"timeout": 41, "cameras": {"CAM001": {"enc_key": "OLD", "future_field": True}}},
    )
    entered, release = Event(), Event()
    device_keys = []

    def work(*_args, **kwargs):
        entered.set()
        assert release.wait(5)
        device_keys.append(kwargs.get("new_password", "NEW"))
        return "NEW"

    api.set_video_enc = work
    api.get_cam_key = work
    entity = text.EzvizEncryptionKeyText(data, "CAM001", entry)
    entity.hass = hass
    entity.entity_id = "text.synthetic_key"
    data.data["CAM001"]["encrypted_pwd_hash"] = return_password_hash("NEW")
    task = asyncio.create_task(
        entity.async_set_value("NEW")
        if operation == "change"
        else data.credentials.async_fetch("CAM001")
    )
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        close = asyncio.create_task(data.async_close())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if close is not None:
            await close
        else:
            await data.async_close()
    assert device_keys == ["NEW"]
    assert entry.options["cameras"]["CAM001"] == {"enc_key": "NEW", "future_field": True}
    assert entity.current_enc_key_hash == return_password_hash("NEW")
    assert entity.state == ""
    api.close_session.assert_called_once_with()


async def test_queued_key_changes_use_the_last_committed_key(hass):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    entered, release = Event(), Event()
    calls = []

    def change(**kwargs):
        calls.append((kwargs["old_password"], kwargs["new_password"]))
        if kwargs["new_password"] == "FIRST":
            entered.set()
            assert release.wait(5)

    api.set_video_enc = change
    first = asyncio.create_task(data.credentials.async_change("CAM001", "FIRST"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(data.credentials.async_change("CAM001", "SECOND"))
        await asyncio.sleep(0)
        assert calls == [("OLD", "FIRST")]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
        if second is not None:
            await second
        await data.async_close()
    assert calls == [("OLD", "FIRST"), ("FIRST", "SECOND")]
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "SECOND"


async def test_failed_device_change_does_not_commit_and_retry_recovers(hass):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    api.set_video_enc = MagicMock(side_effect=[PyEzvizError("synthetic"), None])
    with pytest.raises(PyEzvizError):
        await data.credentials.async_change("CAM001", "NEW")
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "OLD"
    await data.credentials.async_change("CAM001", "NEW")
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "NEW"
    await data.async_close()


@pytest.mark.parametrize("phase", ["push", "http", "persistence"])
async def test_cleanup_failures_retain_ownership_drain_writes_and_retry(hass, phase):
    data = SimpleNamespace(begin_shutdown=MagicMock(), async_close=AsyncMock())
    push = SimpleNamespace(async_wait_stopped=AsyncMock())
    persistence = SimpleNamespace(async_drain=AsyncMock())
    failing = {
        "push": push.async_wait_stopped,
        "http": data.async_close,
        "persistence": persistence.async_drain,
    }[phase]
    failing.side_effect = [OSError("synthetic"), None]
    owner = runtime.RuntimeCleanup(hass, data, push=push, persistence=persistence)
    with pytest.raises(OSError):
        await owner.async_stop()
    assert not owner.cleanup_complete
    assert owner.task.done()
    assert owner.cleanup_error_type == "OSError"
    persistence.async_drain.assert_awaited_once()
    if phase == "push":
        data.async_close.assert_not_awaited()
    await owner.async_stop()
    assert owner.cleanup_complete
    assert owner.cleanup_error_type is None
    assert push.async_wait_stopped.await_count == (2 if phase == "push" else 1)
    assert data.async_close.await_count == (2 if phase == "http" else 1)
    assert persistence.async_drain.await_count == 2


async def test_setup_retries_failed_owner_without_opening_an_overlapping_client(hass, monkeypatch):
    entry, api, data, _, _, _ = entities(hass)
    owner = runtime.RuntimeCleanup(hass, data)
    api.close_session.side_effect = [OSError("first"), OSError("second"), None]
    hass.data[controls.DOMAIN] = {"_runtimes": {entry.entry_id: owner}}
    with pytest.raises(OSError):
        await owner.async_stop()
    factory = MagicMock(side_effect=RuntimeError("new client now permitted"))
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", factory)
    with pytest.raises(ConfigEntryNotReady):
        await ezviz_cloud.async_setup_entry(hass, entry)
    factory.assert_not_called()
    assert not owner.cleanup_complete
    with pytest.raises(RuntimeError, match="new client now permitted"):
        await ezviz_cloud.async_setup_entry(hass, entry)
    assert owner.cleanup_complete
    factory.assert_called_once()
    assert api.close_session.call_count == 3


async def test_shutdown_rejects_queued_rotation_without_losing_active_commit(hass):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    entered, release = Event(), Event()
    calls = []

    def change(**kwargs):
        calls.append(kwargs["new_password"])
        entered.set()
        assert release.wait(5)

    api.set_video_enc = change
    first = asyncio.create_task(data.credentials.async_change("CAM001", "FIRST"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(data.credentials.async_change("CAM001", "SECOND"))
        await asyncio.sleep(0)
        data.begin_shutdown()
    finally:
        release.set()
        await first
        if second is not None:
            with pytest.raises(HomeAssistantError, match="shutting down"):
                await second
        await data.async_close()
    assert calls == ["FIRST"]
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "FIRST"


async def test_cancelled_rotation_survives_actual_ha_stop_and_disk_restart(hass):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    owner = runtime.RuntimeCleanup(hass, data)
    entry.runtime_data = owner
    entered, release = Event(), Event()

    def change(**_kwargs):
        entered.set()
        assert release.wait(5)

    api.set_video_enc = change

    async def shutdown(_event):
        await owner.async_wait_stopped()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    task = asyncio.create_task(data.credentials.async_change("CAM001", "NEW"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hass.loop.call_later(0.05, release.set)
        await hass.async_stop()
    finally:
        release.set()
    assert owner.cleanup_complete
    restored = HomeAssistant(hass.config.config_dir)
    restored.config_entries = ConfigEntries(restored, {})
    try:
        await restored.config_entries.async_initialize()
        saved = restored.config_entries.async_get_entry(entry.entry_id)
        assert saved is not None
        assert saved.options["cameras"]["CAM001"]["enc_key"] == "NEW"
    finally:
        await restored.async_stop()


async def test_removal_retains_failed_cleanup_and_never_resurrects_late_credentials(
    hass, monkeypatch
):
    entry, api, data, _, _, _ = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "OLD"}}}
    )
    owner = runtime.RuntimeCleanup(hass, data)
    entry.runtime_data = owner
    owners = {entry.entry_id: owner}
    hass.data[controls.DOMAIN] = {"_runtimes": owners}
    entered, release = Event(), Event()
    api.close_session.side_effect = [OSError("close retry"), None]
    monkeypatch.setattr(runtime, "CLEANUP_RETRY_SECONDS", 0.001)

    def change(**_kwargs):
        entered.set()
        assert release.wait(5)

    api.set_video_enc = change
    task = asyncio.create_task(data.credentials.async_change("CAM001", "NEW"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await hass.config_entries.async_remove(entry.entry_id)
        assert hass.config_entries.async_get_entry(entry.entry_id) is None
        assert owners[entry.entry_id] is owner
        assert not owner.cleanup_complete
    finally:
        release.set()
        await task
        await owner.async_wait_stopped()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.entry_id not in owners
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "OLD"
    assert owner.cleanup_complete
    assert api.close_session.call_count == 2
