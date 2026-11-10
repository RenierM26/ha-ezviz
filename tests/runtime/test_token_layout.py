"""Private-only SDK snapshots: migration, storage failures and stale-writer races."""

import asyncio
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock

import atomicwrites
import pytest
import test_config_flow as controls

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import config_flow, tokens
from custom_components.ezviz_cloud.const import ATTR_TYPE_CLOUD, CONF_AUTH_REF, DOMAIN
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

integration = controls.integration
sdk = controls.sdk

OPAQUE = {
    "session_id": "saved-session",
    "rf_session_id": "saved-refresh",
    "username": "internal",
    "user_id": "official-user",
    "feature_code": "host-fixture",
    "hardware_code": "hardware-fixture",
    "push_profile": "profile-fixture",
    "push_state": {"counter": 7, "key": "private-fixture"},
    "service_urls": {"pushDasDomain": "push.example.invalid", "pushDasPort": 8666},
    "api_url": "api.example.invalid",
    "future_sdk_field": {"preserve": True},
}


def legacy_entry(hass, *, unique_id="fixture@example.invalid"):
    return controls.add_entry(
        hass,
        version=4,
        unique_id=unique_id,
        data={
            "type": ATTR_TYPE_CLOUD,
            "url": "api.example.invalid",
            "username": "fixture@example.invalid",
            "session_id": "obsolete-session",
            "rf_session_id": "obsolete-refresh",
            "user_id": "internal",
            "sdk_token": deepcopy(OPAQUE),
        },
    )


async def test_v4_migration_prefers_latest_private_snapshot_and_strips_duplicates(hass):
    entry = legacy_entry(hass)
    owner = tokens.TokenPersistence(hass, entry)
    latest = {**OPAQUE, "session_id": "rotated", "push_state": {"counter": 91, "key": "latest-key"}}
    # Actual old Store envelope, not the new writer pretending to be a migration.
    await owner._store.async_save({"anchor": owner._anchor, "token": latest})
    identity = entry.entry_id, entry.unique_id, entry.title
    options = deepcopy(dict(entry.options))
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.version == 5
    assert entry.data == {"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"}
    assert await tokens.TokenPersistence(hass, entry).async_load() == latest
    assert (entry.entry_id, entry.unique_id, entry.title) == identity
    assert entry.options == options
    record = await owner._store.async_load()
    assert set(record) == {"account", "revision", "token"}
    assert record["token"] == latest
    assert Path(owner._store.path).stat().st_mode & 0o777 == 0o600


async def test_migration_storage_failure_keeps_original_fields_and_retries(hass, monkeypatch):
    entry = legacy_entry(hass)
    before = deepcopy(entry.as_dict())
    original_fsync = atomicwrites._proper_fsync
    with monkeypatch.context() as context:
        context.setattr(
            atomicwrites,
            "_proper_fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
        )
        with pytest.raises(ConfigEntryNotReady):
            await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.as_dict() == before
    assert atomicwrites._proper_fsync is original_fsync
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    assert "sdk_token" not in entry.data
    assert await tokens.TokenPersistence(hass, entry).async_load() == OPAQUE


@pytest.mark.parametrize("seeded", [False, True])
async def test_reauth_supersedes_old_owner_even_when_old_revision_was_unset(hass, seeded):
    entry = legacy_entry(hass)
    old = tokens.TokenPersistence(hass, entry)
    if seeded:
        await old.async_save(OPAQUE)
    assert await old.async_load() == OPAQUE
    fresh = {**OPAQUE, "push_state": {"counter": 0}, "feature_code": "new-host"}
    new = tokens.TokenPersistence(hass, entry)
    await new.async_replace(fresh)
    with pytest.raises(OSError, match="superseded"):
        await old.async_save({**OPAQUE, "push_state": {"counter": 999}})
    assert await old.async_load() is None
    assert await tokens.TokenPersistence(hass, entry).async_load() == fresh
    assert entry.data == {"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"}


async def test_inflight_old_save_finishes_before_reauth_and_cannot_overwrite_it(hass, monkeypatch):
    entry = legacy_entry(hass)
    old = tokens.TokenPersistence(hass, entry)
    await old.async_save(OPAQUE)
    entered, release = Event(), Event()
    original = atomicwrites._proper_fsync

    def stall(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", stall)
    task = hass.async_add_executor_job(
        old.save_from_worker, {**OPAQUE, "push_state": {"counter": 8}}
    )
    replacement = None
    fresh = {**OPAQUE, "session_id": "fresh", "push_state": {"counter": 0}}
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        replacement = asyncio.create_task(tokens.TokenPersistence(hass, entry).async_replace(fresh))
        await asyncio.sleep(0)
        assert not replacement.done()
    finally:
        release.set()
        await task
        if replacement:
            await replacement
        monkeypatch.setattr(atomicwrites, "_proper_fsync", original)
    assert await tokens.TokenPersistence(hass, entry).async_load() == fresh
    with pytest.raises(OSError):
        await old.async_save(OPAQUE)


async def test_disk_restart_between_private_promotion_and_entry_update_does_not_roll_back(
    hass, monkeypatch
):
    entry = legacy_entry(hass)
    hass.config_entries.async_update_entry(entry, title="Persist legacy metadata")
    latest = {**OPAQUE, "push_state": {"counter": 95}}
    await tokens.TokenPersistence(hass, entry).async_save(latest)
    original = hass.config_entries.async_update_entry

    def fail_metadata(target, **kwargs):
        if kwargs.get("version") == 5:
            raise OSError("metadata update interrupted")
        return original(target, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(hass.config_entries, "async_update_entry", fail_metadata)
        with pytest.raises(ConfigEntryNotReady):
            await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.version == 4 and "sdk_token" in entry.data
    await hass.async_stop()
    restored = HomeAssistant(hass.config.config_dir)
    restored.config_entries = ConfigEntries(restored, {})
    try:
        await restored.config_entries.async_initialize()
        saved = restored.config_entries.async_get_entry(entry.entry_id)
        assert saved.version == 4
        owner = tokens.TokenPersistence(restored, saved)
        assert await owner.async_load() == latest
        await owner.async_promote(version=5)
        assert saved.version == 5
        assert set(saved.data) == {"type", "url"}
        assert await owner.async_load() == latest
    finally:
        await restored.async_stop()


@pytest.mark.parametrize("reauth", [False, True])
@pytest.mark.parametrize("mfa", [False, True])
async def test_login_storage_errors_preserve_form_and_recover(hass, sdk, monkeypatch, reauth, mfa):
    entry = controls.add_entry(hass) if reauth else None
    manager = hass.config_entries.flow
    result = (
        await manager.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
        )
        if reauth
        else await controls.start_user(hass)
    )
    submission = (
        {"username": controls.USER["username"], "password": "not-stored"}
        if reauth
        else controls.USER
    )
    if mfa:
        sdk.return_value.enable_channel99.side_effect = controls.EzvizAuthVerificationCode()
        result = await manager.async_configure(result["flow_id"], submission)
        sdk.return_value.enable_channel99.side_effect = None
        submission = {"sms_code": "123456"}
    step = result["step_id"]
    with monkeypatch.context() as context:
        if reauth:
            context.setattr(
                tokens.TokenPersistence,
                "async_replace",
                AsyncMock(side_effect=OSError("storage unavailable")),
            )
        else:
            context.setattr(
                config_flow, "stage_login", AsyncMock(side_effect=OSError("storage unavailable"))
            )
        result = await manager.async_configure(result["flow_id"], submission)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    assert result["errors"] == {"base": "cannot_save_credentials"}
    result = await manager.async_configure(result["flow_id"], submission)
    if reauth:
        assert result["reason"] == "reauth_successful"
        saved = entry
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        saved = result["result"]
    assert set(saved.data) == {"type", "url"}
    assert await tokens.TokenPersistence(hass, saved).async_load() == controls.TOKEN


async def test_create_hook_keeps_private_reference_when_promotion_must_retry(
    hass, sdk, monkeypatch
):
    result = await controls.start_user(hass)
    original = tokens.AcknowledgedStore.async_save

    async def fail_canonical(store, record):
        if ".token." in store.key:
            raise OSError("canonical store unavailable")
        await original(store, record)

    with monkeypatch.context() as context:
        context.setattr(tokens.AcknowledgedStore, "async_save", fail_canonical)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], controls.USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert set(entry.data) == {"type", "url", CONF_AUTH_REF}
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN
    assert set(entry.data) == {"type", "url"}
    assert not list(Path(hass.config.config_dir, ".storage").glob(f"{DOMAIN}.pending_auth.*"))


async def test_cancelled_initial_stage_removes_its_unreferenced_private_file(hass, monkeypatch):
    entered, release = Event(), Event()
    original = atomicwrites._proper_fsync

    def stall(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", stall)
    task = asyncio.create_task(tokens.stage_login(hass, "fixture@example.invalid", OPAQUE))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        monkeypatch.setattr(atomicwrites, "_proper_fsync", original)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not list(Path(hass.config.config_dir, ".storage").glob(f"{DOMAIN}.pending_auth.*"))


async def test_legacy_missing_unique_id_keeps_only_the_necessary_login_name(hass, sdk):
    entry = legacy_entry(hass, unique_id=None)
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    assert set(entry.data) == {"type", "url", "username"}
    assert entry.data["username"] == "fixture@example.invalid"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    assert result["data_schema"]({"password": "not-stored"})["username"] == entry.data["username"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": entry.data["username"], "password": "not-stored"}
    )
    assert result["reason"] == "reauth_successful"
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN


async def test_unknown_legacy_login_name_is_retained_after_fresh_reauth(hass, sdk):
    entry = controls.add_entry(
        hass, unique_id=None, data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "fixture@example.invalid", "password": "not-stored"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["username"] == "fixture@example.invalid"
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN


async def test_missing_private_credentials_require_reauth_without_a_public_fallback(
    hass, monkeypatch
):
    entry = controls.add_entry(
        hass, version=5, data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"}
    )
    factory = AsyncMock()
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", factory)
    with pytest.raises(ConfigEntryAuthFailed):
        await ezviz_cloud.async_setup_entry(hass, entry)
    factory.assert_not_called()
    assert set(entry.data) == {"type", "url"}


async def test_minimal_entry_reauth_reloads_even_when_public_data_is_unchanged(hass, sdk):
    entry = controls.add_entry(
        hass, version=5, data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"}
    )
    await tokens.TokenPersistence(hass, entry).async_save(OPAQUE)
    before = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=before
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": entry.unique_id, "password": "not-stored"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data == before
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


async def test_new_entry_has_one_private_snapshot_and_no_auth_dump_after_restart(hass, sdk):
    sdk.return_value.enable_channel99.return_value = deepcopy(OPAQUE)
    result = await controls.start_user(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], controls.USER)
    entry = result["result"]
    assert entry.data == {"type": ATTR_TYPE_CLOUD, "url": controls.REGION_URLS["eu"]}
    storage = Path(hass.config.config_dir, ".storage")
    assert len(list(storage.glob(f"{DOMAIN}.token.*"))) == 1
    assert not list(storage.glob(f"{DOMAIN}.pending_auth.*"))
    await hass.async_stop()
    restored = HomeAssistant(hass.config.config_dir)
    restored.config_entries = ConfigEntries(restored, {})
    try:
        await restored.config_entries.async_initialize()
        saved = restored.config_entries.async_get_entry(entry.entry_id)
        assert saved.version == 5
        assert saved.data == entry.data
        assert await tokens.TokenPersistence(restored, saved).async_load() == OPAQUE
    finally:
        await restored.async_stop()


@pytest.mark.parametrize("remove", [False, True])
async def test_pending_login_is_not_left_behind_by_reauth_or_removal(hass, remove):
    reference = await tokens.stage_login(hass, controls.USER["username"], OPAQUE)
    entry = controls.add_entry(
        hass,
        version=5,
        data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid", CONF_AUTH_REF: reference},
    )
    owner = tokens.TokenPersistence(hass, entry)
    if remove:
        await owner.async_remove()
    else:
        await owner.async_replace(controls.TOKEN)
        assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN
        assert set(entry.data) == {"type", "url"}
    assert not list(Path(hass.config.config_dir, ".storage").glob(f"{DOMAIN}.pending_auth.*"))


async def test_failed_pending_file_removal_keeps_retry_reference(hass, monkeypatch):
    reference = await tokens.stage_login(hass, controls.USER["username"], OPAQUE)
    entry = controls.add_entry(
        hass,
        version=5,
        data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid", CONF_AUTH_REF: reference},
    )
    owner = tokens.TokenPersistence(hass, entry)
    original = tokens.AcknowledgedStore.async_remove

    async def fail_pending(store):
        if ".pending_auth." in store.key:
            raise OSError("staged file cleanup unavailable")
        await original(store)

    with monkeypatch.context() as context:
        context.setattr(tokens.AcknowledgedStore, "async_remove", fail_pending)
        with pytest.raises(OSError):
            await owner.async_load()
    assert entry.data[CONF_AUTH_REF] == reference
    assert await owner.async_load() == OPAQUE
    assert set(entry.data) == {"type", "url"}
    assert not list(Path(hass.config.config_dir, ".storage").glob(f"{DOMAIN}.pending_auth.*"))


async def test_unknown_legacy_account_survives_interrupted_login_name_update(hass, monkeypatch):
    entry = controls.add_entry(
        hass,
        unique_id=None,
        version=5,
        data={"type": ATTR_TYPE_CLOUD, "url": "api.example.invalid"},
    )
    owner = tokens.TokenPersistence(hass, entry)
    with monkeypatch.context() as context:
        context.setattr(
            hass.config_entries,
            "async_update_entry",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata interrupted")),
        )
        with pytest.raises(OSError):
            await owner.async_replace(OPAQUE, login_name="fixture@example.invalid")
    # Bind by immutable entry ID, not a newly entered legacy login name.
    assert await tokens.TokenPersistence(hass, entry).async_load() == OPAQUE
    await owner.async_replace(controls.TOKEN, login_name="fixture@example.invalid")
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN


async def test_foreign_account_record_cannot_be_loaded_or_overwritten(hass):
    entry = legacy_entry(hass)
    owner = tokens.TokenPersistence(hass, entry)
    record = {"account": "another-account", "revision": "another-login", "token": OPAQUE}
    await owner._store.async_save(record)
    assert await owner.async_load() is None
    with pytest.raises(OSError, match="another account"):
        await owner.async_save(controls.TOKEN)
    assert await owner._store.async_load() == record
    # Explicit fresh reauthentication may replace an unusable record.
    await owner.async_replace(controls.TOKEN)
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN


async def test_cancelled_reauth_finishes_private_commit_and_schedules_reload(
    hass, sdk, monkeypatch
):
    entry = legacy_entry(hass)
    await ezviz_cloud.async_migrate_entry(hass, entry)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    entered, release = Event(), Event()
    original = atomicwrites._proper_fsync

    def stall(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(atomicwrites, "_proper_fsync", stall)
    task = asyncio.create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": entry.unique_id, "password": "not-stored"}
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        monkeypatch.setattr(atomicwrites, "_proper_fsync", original)
    assert await tokens.TokenPersistence(hass, entry).async_load() == controls.TOKEN
    assert set(entry.data) == {"type", "url"}
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
