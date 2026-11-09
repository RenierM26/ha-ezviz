"""Historical fixtures through real HA entries, registries and Repairs (no SDK IO)."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud.config_flow import EzvizConfigFlow
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.migration import migrate_unique_ids_with_coordinator
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

FIXTURES = Path(__file__).parent / "fixtures" / "migration"


@pytest.fixture(autouse=True)
def integration_loader(hass):
    """Resolve the actual custom component for HA's real removal callbacks."""
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration


def add_entry(hass, **overrides):
    """Real entry; registration mirrors HA's test helper, not a mocked manager."""
    fields = dict(
        data={},
        discovery_keys={},
        domain=DOMAIN,
        minor_version=1,
        options={},
        source="user",
        state=ConfigEntryState.NOT_LOADED,
        subentries_data=(),
        title="Fixture",
        unique_id=None,
        version=3,
    )
    fields.update(overrides)
    entry = ConfigEntry(**fields)
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture(params=[1, 2, 3, 4], ids=lambda v: f"v{v}")
def historical(hass, request):
    """Synthetic values, historically sourced field sets; see fixtures README."""
    fixture = json.loads((FIXTURES / f"v{request.param}.json").read_text())
    entries = [add_entry(hass, **fields) for fields in fixture["entries"]]
    return entries, fixture["expected_cameras"]


async def test_historical_upgrade_preserves_identity_and_credentials(hass, historical):
    entries, expected = historical
    cloud = entries[0]
    original_data = deepcopy(dict(cloud.data))
    identity = cloud.entry_id, cloud.unique_id, cloud.title, cloud.minor_version
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=cloud.entry_id, identifiers={(DOMAIN, "SERIAL001")}
    )
    entity = registry.async_get_or_create(
        "camera",
        DOMAIN,
        "SERIAL001",
        config_entry=cloud,
        device_id=device.id,
        suggested_object_id="custom_front_door",
    )
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.version == EzvizConfigFlow.VERSION == ezviz_cloud.TARGET_VERSION
    assert dict(cloud.data) == original_data
    assert cloud.options == {"timeout": 41, "cameras": expected}
    assert (cloud.entry_id, cloud.unique_id, cloud.title, cloud.minor_version) == identity
    assert registry.async_get(entity.entity_id) == entity
    assert dr.async_get(hass).async_get(device.id) == device
    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [cloud.entry_id]
    before = cloud.as_dict()
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.as_dict() == before


@pytest.mark.parametrize("version", [5, 99])
async def test_future_major_is_rejected_unchanged(hass, version):
    entry = add_entry(hass, version=version, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    before = entry.as_dict()
    assert not await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_existing_options_win_and_other_entries_are_untouched(hass):
    cloud = add_entry(
        hass,
        data={"type": "EZVIZ_CLOUD_ACCOUNT"},
        options={
            "timeout": 17,
            "other_option": True,
            "cameras": {"SERIAL001": {"password": "new-choice"}},
        },
    )
    add_entry(
        hass, data={"type": "CAMERA_ACCOUNT", "password": "old-choice"}, unique_id="SERIAL001"
    )
    others = [
        add_entry(hass, domain="other", source="ignore"),
        add_entry(hass, version=4, source="ignore"),
        add_entry(hass, version=4, data={"type": "CAMERA_ACCOUNT"}, unique_id="CURRENT"),
        add_entry(hass, data={"type": "unrecognized"}),
    ]
    before = [entry.as_dict() for entry in others]
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.options == {
        "timeout": 17,
        "other_option": True,
        "cameras": {"SERIAL001": {"password": "new-choice"}},
    }
    assert [entry.as_dict() for entry in others] == before
    assert all(hass.config_entries.async_get_entry(e.entry_id) is e for e in others)


@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_removal_failure_is_retryable_without_losing_options(hass, monkeypatch, failure):
    cloud = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    camera = add_entry(hass, data={"type": "CAMERA_ACCOUNT"}, unique_id="SERIAL001")
    original_remove = hass.config_entries.async_remove
    remove = AsyncMock(
        return_value=False, side_effect=OSError("synthetic") if failure == "exception" else None
    )
    monkeypatch.setattr(hass.config_entries, "async_remove", remove)
    assert not await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.version == 3
    assert hass.config_entries.async_get_entry(camera.entry_id) is camera
    assert cloud.options["cameras"]["SERIAL001"]["username"] == "admin"
    monkeypatch.setattr(hass.config_entries, "async_remove", original_remove)
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.version == 4
    assert hass.config_entries.async_get_entry(camera.entry_id) is None


@pytest.mark.parametrize("serial", [None, ""])
async def test_missing_camera_serial_fails_without_changes(hass, serial):
    cloud = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    camera = add_entry(hass, data={"type": "CAMERA_ACCOUNT"}, unique_id=serial)
    before = [e.as_dict() for e in (cloud, camera)]
    assert not await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert [e.as_dict() for e in (cloud, camera)] == before


async def test_ambiguous_accounts_do_not_claim_camera_credentials(hass):
    first = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    second = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    camera = add_entry(hass, data={"type": "CAMERA_ACCOUNT"}, unique_id="SERIAL001")
    before = [e.as_dict() for e in (first, second, camera)]
    assert not await ezviz_cloud.async_migrate_entry(hass, first)
    assert [e.as_dict() for e in (first, second, camera)] == before


@pytest.mark.parametrize(
    "platform,key",
    [("sensor", "Seconds_Last_Trigger"), ("binary_sensor", "Motion_Trigger"), ("switch", "PRIVACY")],
)
@pytest.mark.parametrize("variant", ["canonical", "lower", "upper", "rename"])
async def test_unique_id_upgrade_keeps_user_entity_settings(hass, platform, key, variant):
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old_key = {"canonical": key, "lower": key.lower(), "upper": key.upper(), "rename": "old_key"}[
        variant
    ]
    entity = registry.async_get_or_create(
        platform,
        DOMAIN,
        f"SERIAL001_Front.Door_1.{old_key}",
        config_entry=entry,
        suggested_object_id="user_chosen",
    )
    entity = registry.async_update_entity(
        entity.entity_id,
        name="My custom name",
        icon="mdi:home",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    before = entity.as_partial_dict
    stats = await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data={"SERIAL001": {"name": "Front.Door_1"}}),
        platform_domain=platform,
        allowed_keys=[key],
        key_renames={"old_key": key},
    )
    updated = registry.async_get(entity.entity_id)
    assert stats.migrated == 1
    assert updated.unique_id == f"SERIAL001_{key}"
    assert updated.id == entity.id
    assert updated.name == entity.name and updated.icon == entity.icon
    assert updated.disabled_by == entity.disabled_by
    assert updated.config_entry_id == entry.entry_id
    assert before["entity_id"] == updated.entity_id
    again = await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data={"SERIAL001": {"name": "Front.Door_1"}}),
        platform_domain=platform,
        allowed_keys=[key],
    )
    assert again.migrated == 0


async def test_collision_unmapped_scoping_and_repair_lifecycle(hass):
    entry, other = add_entry(hass), add_entry(hass)
    registry = er.async_get(hass)
    target = registry.async_get_or_create(
        "sensor", DOMAIN, "SERIAL001_battery_level", config_entry=entry
    )
    legacy = registry.async_get_or_create(
        "sensor", DOMAIN, "SERIAL001_Front.battery_level", config_entry=entry
    )
    unmapped = registry.async_get_or_create(
        "sensor", DOMAIN, "SERIAL001_OldName.battery_level", config_entry=entry
    )
    unrelated = [
        registry.async_get_or_create(
            "sensor", DOMAIN, "SERIAL001_Front.wifi_ssid", config_entry=other
        ),
        registry.async_get_or_create(
            "switch", DOMAIN, "SERIAL001_Front.privacy", config_entry=entry
        ),
        registry.async_get_or_create(
            "sensor", "other", "SERIAL001_Front.wifi_ssid", config_entry=entry
        ),
    ]

    async def migrate():
        return await migrate_unique_ids_with_coordinator(
            hass,
            entry,
            SimpleNamespace(data={"SERIAL001": {"name": "Front"}}),
            platform_domain="sensor",
            allowed_keys=["battery_level", "wifi_ssid"],
        )

    stats = await migrate()
    assert (stats.skipped_collision, stats.skipped_unmapped) == (1, 1)
    issue_id = f"uid_migration_review_sensor_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.translation_key == "uid_migration_review"
    assert issue.severity == ir.IssueSeverity.WARNING
    assert legacy.entity_id in issue.translation_placeholders["examples"]
    assert all(registry.async_get(e.entity_id) == e for e in unrelated)
    registry.async_remove(target.entity_id)
    registry.async_remove(unmapped.entity_id)
    assert (await migrate()).migrated == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # A previously reported issue must also clear when the user resolved all legacy IDs.
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="uid_migration_review",
    )
    await migrate()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    "data", [None, {}, {"SERIAL001": {"name": None}}, {"SERIAL001": {"name": "different"}}]
)
async def test_unavailable_or_renamed_camera_does_not_guess_unique_ids(hass, data):
    entry = add_entry(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor", DOMAIN, "SERIAL001_Front.battery_level", config_entry=entry
    )
    stats = await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data=data),
        platform_domain="sensor",
        allowed_keys=["battery_level"],
    )
    assert stats.skipped_unmapped == 1
    assert registry.async_get(legacy.entity_id) == legacy


async def test_repair_clears_with_no_legacy_ids_anywhere(hass):
    entry = add_entry(hass)
    issue_id = f"uid_migration_review_sensor_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="uid_migration_review",
    )
    await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data={}),
        platform_domain="sensor",
        allowed_keys=["battery_level"],
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    "version,key", [(1, None), (2, None), (2, ""), (2, "separate"), (3, None), (3, "")]
)
async def test_historical_encryption_and_rtsp_defaults(hass, version, key):
    cloud = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"}, version=version)
    data = {"type": "CAMERA_ACCOUNT", "password": "verification"}
    if key is not None:
        data["enc_key"] = key
    add_entry(hass, version=version, data=data, unique_id="SERIAL001")
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    result = cloud.options["cameras"]["SERIAL001"]
    expected = (
        "verification"
        if version == 1 or (version == 2 and key == "")
        else "fetch_my_key"
        if key is None
        else key
    )
    assert result == {
        "username": "admin",
        "password": "verification",
        "enc_key": expected,
        "rtsp_uses_verification_code": version <= 2,
        "ffmpeg_arguments": "/Streaming/Channels/102",
    }


async def test_ignored_typed_camera_is_not_imported(hass):
    cloud = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    ignored = add_entry(hass, source="ignore", data={"type": "CAMERA_ACCOUNT"}, unique_id="IGNORED")
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.options == {"timeout": 25, "cameras": {}}
    assert hass.config_entries.async_get_entry(ignored.entry_id) is None


async def test_partial_removal_then_cancellation_can_be_retried(hass, monkeypatch):
    cloud = add_entry(hass, data={"type": "EZVIZ_CLOUD_ACCOUNT"})
    first = add_entry(hass, data={"type": "CAMERA_ACCOUNT", "password": "first"}, unique_id="FIRST")
    second = add_entry(
        hass, data={"type": "CAMERA_ACCOUNT", "password": "second"}, unique_id="SECOND"
    )
    remove = hass.config_entries.async_remove

    async def interrupted(entry_id):
        if entry_id == second.entry_id:
            raise asyncio.CancelledError
        return await remove(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_remove", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.version == 3
    assert hass.config_entries.async_get_entry(first.entry_id) is None
    assert hass.config_entries.async_get_entry(second.entry_id) is second
    assert cloud.options["cameras"]["FIRST"]["password"] == "first"
    monkeypatch.setattr(hass.config_entries, "async_remove", remove)
    assert await ezviz_cloud.async_migrate_entry(hass, cloud)
    assert cloud.options["cameras"]["FIRST"]["password"] == "first"
    assert cloud.options["cameras"]["SECOND"]["password"] == "second"


@pytest.mark.parametrize(
    "source,etype", [("user", "CAMERA_ACCOUNT"), ("ignore", None), ("user", "unknown")]
)
async def test_placeholder_migration_is_non_destructive_before_account(hass, source, etype):
    entry = add_entry(hass, data={"type": etype}, source=source)
    before = entry.as_dict()
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_latest_minor_and_full_token_are_untouched(hass):
    entry = add_entry(
        hass,
        version=4,
        minor_version=9,
        data={"type": "EZVIZ_CLOUD_ACCOUNT", "sdk_token": {"push_state": {"counter": 7}}},
        options={"cameras": {}, "timeout": 61},
    )
    before = entry.as_dict()
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_presence_check_and_unsupported_rename_remain_unmapped(hass):
    entry = add_entry(hass)
    registry = er.async_get(hass)
    rows = [
        registry.async_get_or_create("sensor", DOMAIN, f"SERIAL001_Front.{key}", config_entry=entry)
        for key in ("battery_level", "old_key", "wifi_ssid")
    ]
    stats = await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data={"SERIAL001": {"name": "Front", "wifi_ssid": "fixture"}}),
        platform_domain="sensor",
        allowed_keys=["battery_level", "wifi_ssid"],
        key_renames={"old_key": "not_allowed"},
        presence_check=lambda key, data: key in data,
    )
    assert (stats.migrated, stats.skipped_unmapped) == (1, 2)
    assert registry.async_get(rows[0].entity_id) == rows[0]
    assert registry.async_get(rows[1].entity_id) == rows[1]
    assert registry.async_get(rows[2].entity_id).unique_id == "SERIAL001_wifi_ssid"


async def test_repair_examples_are_bounded_and_sorted(hass):
    entry = add_entry(hass)
    registry = er.async_get(hass)
    rows = [
        registry.async_get_or_create(
            "sensor", DOMAIN, f"SERIAL{i}_Unknown.battery_level", config_entry=entry
        )
        for i in range(25)
    ]
    stats = await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        SimpleNamespace(data={}),
        platform_domain="sensor",
        allowed_keys=["battery_level"],
    )
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"uid_migration_review_sensor_{entry.entry_id}"
    )
    assert stats.skipped_unmapped == 25
    assert issue.translation_placeholders["examples"].splitlines() == [
        f"- {entity_id}" for entity_id in sorted(row.entity_id for row in rows)[:20]
    ]


async def test_consolidation_survives_actual_ha_storage_restart(hass, historical):
    entries, expected = historical
    cloud = entries[0]
    # Registration alone does not persist the already-current v4 fixture.
    hass.config_entries.async_update_entry(cloud, title="Persisted fixture account")
    assert await cloud.async_migrate(hass)
    # async_update_entry schedules HA's own Store save; stopping flushes it.
    await hass.async_stop()
    restored = HomeAssistant(hass.config.config_dir)
    restored.config_entries = ConfigEntries(restored, {})
    try:
        await restored.config_entries.async_initialize()
        saved = restored.config_entries.async_get_entry(cloud.entry_id)
        assert saved is not None
        assert saved.version == 4
        assert saved.options == {"timeout": 41, "cameras": expected}
        assert saved.data == cloud.data
        assert len(restored.config_entries.async_entries(DOMAIN)) == 1
    finally:
        await restored.async_stop()


@pytest.mark.parametrize("version", [1, 2])
async def test_old_token_without_internal_user_id_requires_reauth(hass, version):
    fixture = json.loads((FIXTURES / f"v{version}.json").read_text())
    entry = add_entry(hass, **fixture["entries"][0])
    assert "user_id" not in entry.data
    assert await ezviz_cloud.async_migrate_entry(hass, entry)
    # The login email is not the SDK's internal user ID. Do not invent one or
    # silently send incomplete credentials to the cloud; use HA's reauth path.
    with pytest.raises(ConfigEntryAuthFailed, match="Missing EZVIZ token fields"):
        await ezviz_cloud.async_setup_entry(hass, entry)
    assert "user_id" not in entry.data
