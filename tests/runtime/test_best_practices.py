"""Real HA credential state, entity renames, typed ownership and repair recovery."""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import image, repairs, text, views
from custom_components.ezviz_cloud.const import DOMAIN, SIGNAL_PICTURE_CAPTURED
from homeassistant import loader
from homeassistant.components.repairs import (
    async_setup as async_setup_repairs,
    repairs_flow_manager,
    websocket_api as repairs_websocket,
)
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import (
    entity as entity_module,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import EntityPlatform
from tests.runtime.test_hardening import entities, entry_for


@pytest.fixture(autouse=True)
def integration(hass):
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration


@pytest.fixture
async def repair_manager(hass, monkeypatch):
    """Let HA initialize its own Repairs domain data across supported/dev layouts."""
    # HTTP view registration is outside these repair-flow tests. Keep real
    # platform loading, manager, issue registry and config-entry retry behavior.
    monkeypatch.setattr(repairs_websocket, "async_setup", MagicMock())
    hass.config.components.add(DOMAIN)
    assert await async_setup_repairs(hass, {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return manager


async def test_key_is_write_only_and_images_use_options_not_state(hass):
    entry, api, data, _, _, alarm = entities(hass)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"enc_key": "PRIVATE_OLD"}}}
    )
    api.set_video_enc = MagicMock()
    key = text.EzvizEncryptionKeyText(data, "CAM001", entry)
    key._attr_entity_registry_enabled_default = True
    key._attr_should_poll = False
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="text",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([key])
    key.async_write_ha_state()
    assert key.native_value == key.state == ""
    assert "PRIVATE_OLD" not in str(hass.states.async_all())
    await key.async_set_value("PRIVATE_NEW")
    key.async_write_ha_state()
    assert key.state == ""
    assert "PRIVATE_NEW" not in str(hass.states.async_all())
    api.set_video_enc.assert_called_once_with(
        serial="CAM001", enable=2, new_password="PRIVATE_NEW", old_password="PRIVATE_OLD"
    )
    hass.states.async_set("text.fake_key", "WRONG_KEY")
    assert views._encryption_key(hass, entry, "CAM001") == "PRIVATE_NEW"
    alarm._sync_encryption_key()
    assert alarm.alarm_image_password == "PRIVATE_NEW"
    assert not hasattr(key, "async_get_last_state")
    assert not hasattr(alarm, "cam_key_entity_id")
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "PRIVATE_NEW"
    api.get_cam_key = MagicMock(return_value="PRIVATE_FETCHED")
    data.data["CAM001"]["encrypted_pwd_hash"] = "changed-device-hash"
    await key.async_update()
    assert key.state == ""
    assert entry.options["cameras"]["CAM001"]["enc_key"] == "PRIVATE_FETCHED"
    alarm._sync_encryption_key()
    assert alarm.alarm_image_password == "PRIVATE_FETCHED"
    await platform.async_reset()
    await data.async_close()


@pytest.mark.parametrize("entity_kind", ["snapshot", "motion", "key", "name"])
async def test_registry_rename_preserves_entity_and_subscriptions(hass, monkeypatch, entity_kind):
    entry, _, data, _, _, _ = entities(hass)
    domain = "image" if entity_kind in ("snapshot", "motion") else "text"
    if entity_kind == "snapshot":
        entity = image.EzvizSnapshot(hass, data, "CAM001", {})
    elif entity_kind == "motion":
        entity = image.EzvizLastMotion(hass, data, "CAM001", {})
    elif entity_kind == "key":
        entity = text.EzvizEncryptionKeyText(data, "CAM001", entry)
        entity._attr_should_poll = False
        entity._attr_entity_registry_enabled_default = True
    else:
        entity = text.EzvizCameraNameText(data, "CAM001")
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain=domain,
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    old_id = entity.entity_id
    assert old_id in platform.entities
    before_listeners = len(data._listeners)
    renamed = f"{domain}.user_renamed_{entity_kind}"
    er.async_get(hass).async_update_entity(old_id, new_entity_id=renamed)
    await hass.async_block_till_done()
    assert entity.entity_id == renamed
    assert platform.entities[renamed] is entity
    assert hass.states.get(old_id) is None
    assert hass.states.get(renamed) is not None
    assert len(data._listeners) == before_listeners
    # Current HA must use in-place renaming; minimum HA legitimately uses remove/add.
    if hasattr(entity_module, "_entity_class_requires_readd"):
        assert not entity_module._entity_class_requires_readd(type(entity))
    if entity_kind == "snapshot":
        async_dispatcher_send(
            hass, SIGNAL_PICTURE_CAPTURED.format("CAM001"), "https://images.example.invalid/new"
        )
        await hass.async_block_till_done()
        assert entity.image_url.endswith("/new")
    await platform.async_reset()
    await data.async_close()


@pytest.mark.parametrize("transient", [False, True])
async def test_cleanup_failure_exception_types_preserve_migration_progress(
    hass, monkeypatch, transient
):
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(entry, version=3)
    victim = entry_for(hass)
    hass.config_entries.async_update_entry(
        victim, data={"type": "CAMERA_ACCOUNT"}, unique_id="CAM001", version=3
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_remove",
        AsyncMock(side_effect=OSError() if transient else RuntimeError()),
    )
    with pytest.raises(ConfigEntryNotReady if transient else ConfigEntryError) as caught:
        await ezviz_cloud.async_migrate_entry(hass, entry)
    assert caught.value.translation_key == "migration_cleanup_failed"
    assert entry.version == 3
    assert "CAM001" in entry.options["cameras"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"migration_{entry.entry_id}")
    assert (issue is None) is transient
    if issue:
        assert issue.is_fixable and issue.data == {"entry_id": entry.entry_id}


async def test_repairs_manager_retries_target_after_user_correction(hass, monkeypatch, repair_manager):
    entry = entry_for(hass, state=ConfigEntryState.MIGRATION_ERROR)
    hass.config_entries.async_update_entry(entry, version=3)
    victim = entry_for(hass)
    hass.config_entries.async_update_entry(victim, version=3, data={"type": "CAMERA_ACCOUNT"})
    with pytest.raises(ConfigEntryError):
        await ezviz_cloud.async_migrate_entry(hass, entry)
    # The user corrects the serial using HA configuration APIs; no guessing.
    hass.config_entries.async_update_entry(victim, unique_id="CAM001")
    # Use real repair manager and retry API; setup is the isolated external boundary.
    manager = repair_manager

    async def setup(entry_id):
        assert entry_id == entry.entry_id
        assert entry.state is ConfigEntryState.NOT_LOADED
        await ezviz_cloud.async_migrate_entry(hass, entry)
        return True

    monkeypatch.setattr(hass.config_entries, "async_setup", setup)
    result = await manager.async_init(DOMAIN, context={"issue_id": f"migration_{entry.entry_id}"})
    assert result["type"] is FlowResultType.FORM
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.version == 5
    assert "CAM001" in entry.options["cameras"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"migration_{entry.entry_id}") is None


@pytest.mark.parametrize("outcome", ["blocked", "setup_retry_after_migration", "removed", "not_failed", "disabled"])
async def test_repair_outcomes_do_not_silently_dismiss_blocked_entries(hass, monkeypatch, outcome, repair_manager):
    entry = entry_for(hass, state=ConfigEntryState.MIGRATION_ERROR if outcome != "not_failed" else ConfigEntryState.NOT_LOADED)
    repairs.create_migration_issue(hass, entry, "migration_missing_serial")
    issue_id = f"migration_{entry.entry_id}"
    manager = repair_manager
    result = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    if outcome == "removed":
        del hass.config_entries._entries[entry.entry_id]
    elif outcome == "disabled":
        # Reload is a separate boundary; a MIGRATION_ERROR entry cannot unload.
        monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)

    async def setup(_entry_id):
        if outcome == "setup_retry_after_migration":
            repairs.clear_migration_issue(hass, entry)
            entry._async_set_state(hass, ConfigEntryState.SETUP_RETRY, None)
        else:
            entry._async_set_state(hass, ConfigEntryState.MIGRATION_ERROR, None)
        return False

    monkeypatch.setattr(hass.config_entries, "async_setup", setup)
    result = await manager.async_configure(result["flow_id"], {})
    if outcome in ("removed", "setup_retry_after_migration"):
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "entry_disabled" if outcome == "disabled" else "retry_failed"}
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
