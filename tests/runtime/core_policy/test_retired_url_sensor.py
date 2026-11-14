"""Retire the raw alarm-image URL sensor without removing internal image sources."""

from collections.abc import AsyncGenerator
from datetime import timedelta
from io import BytesIO
import logging
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components.ezviz_cloud import image, sensor
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.runtime import EzvizConfigEntry, RuntimeCleanup
import httpx
from PIL import Image as PILImage
import pytest

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.test_hardening import entities, entry_for


@pytest.fixture
async def runtime(
    hass: HomeAssistant,
) -> AsyncGenerator[tuple[EzvizConfigEntry, EzvizDataUpdateCoordinator]]:
    """Create a real HA registry/coordinator without an account or device request."""
    loader.async_setup(hass)
    integration = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert integration is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    entry, _, coordinator, *_ = entities(hass)
    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    try:
        yield entry, coordinator
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    "uid",
    [
        "CAM001_last_alarm_pic",
        "CAM001_Synthetic.last_alarm_pic",
        "MISSING_Old.Name.LAST_ALARM_PIC",
        "MISSING_last_alarm_pic",
    ],
)
@pytest.mark.parametrize("disabled", [None, er.RegistryEntryDisabler.USER])
async def test_retire_current_and_legacy_registry_rows_without_recreating_them(
    hass: HomeAssistant,
    runtime: tuple[EzvizConfigEntry, EzvizDataUpdateCoordinator],
    uid: str,
    disabled: er.RegistryEntryDisabler | None,
) -> None:
    """Cleanup works with renamed IDs, disabled rows and devices absent from a poll."""
    entry, _ = runtime
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, uid, config_entry=entry, disabled_by=disabled
    )
    old = registry.async_update_entity(
        old.entity_id, new_entity_id="sensor.user_named_url"
    )
    added = MagicMock()
    await sensor.async_setup_entry(hass, entry, added)
    assert registry.async_get(old.entity_id) is None
    assert all(
        entity.unique_id != "CAM001_last_alarm_pic"
        for call in added.call_args_list
        for entity in call.args[0]
    )
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"uid_migration_review_sensor_{entry.entry_id}"
        )
        is None
    )
    # Repeated setup neither recreates the row nor invents an unmapped-ID repair.
    await sensor.async_setup_entry(hass, entry, MagicMock())
    assert registry.async_get(old.entity_id) is None


async def test_removal_is_account_platform_and_domain_scoped_and_keeps_siblings(
    hass: HomeAssistant,
    runtime: tuple[EzvizConfigEntry, EzvizDataUpdateCoordinator],
) -> None:
    """Do not sweep foreign accounts/platforms, image entities or near-match keys."""
    entry, _ = runtime
    other = entry_for(hass)
    registry = er.async_get(hass)
    retired = registry.async_get_or_create(
        "sensor", DOMAIN, "CAM001_last_alarm_pic", config_entry=entry
    )
    keep = [
        registry.async_get_or_create(
            "sensor", DOMAIN, "OTHER_last_alarm_pic", config_entry=other
        ),
        registry.async_get_or_create(
            "sensor", "other", "CAM001_last_alarm_pic", config_entry=entry
        ),
        registry.async_get_or_create(
            "image", DOMAIN, "CAM001_last_alarm_pic", config_entry=entry
        ),
        registry.async_get_or_create(
            "image", DOMAIN, "CAM001_last_motion_image", config_entry=entry
        ),
        registry.async_get_or_create(
            "image", DOMAIN, "CAM001_snapshot", config_entry=entry
        ),
        registry.async_get_or_create(
            "switch", DOMAIN, "CAM001_last_alarm_pic", config_entry=entry
        ),
        registry.async_get_or_create("sensor", DOMAIN, "ORPHAN_last_alarm_pic"),
        registry.async_get_or_create(
            "sensor", DOMAIN, "CAM001_last_alarm_pic_extra", config_entry=entry
        ),
    ]
    keep.extend(
        [
            registry.async_get_or_create(
                "sensor",
                DOMAIN,
                "CAM001_Synthetic.not_last_alarm_pic",
                config_entry=entry,
            ),
            registry.async_get_or_create(
                "sensor",
                DOMAIN,
                "CAM001_Synthetic.last_alarm_pic_extra",
                config_entry=entry,
            ),
        ]
    )
    sibling = registry.async_get_or_create(
        "sensor", DOMAIN, "CAM001_Synthetic.last_alarm_time", config_entry=entry
    )
    await sensor.async_setup_entry(hass, entry, MagicMock())
    assert registry.async_get(retired.entity_id) is None
    assert all(registry.async_get(row.entity_id) == row for row in keep)
    migrated = registry.async_get(sibling.entity_id)
    assert migrated is not None and migrated.unique_id == "CAM001_last_alarm_time"
    assert migrated.entity_id == sibling.entity_id


async def test_long_signed_url_stays_internal_and_image_entity_still_loads(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[EzvizConfigEntry, EzvizDataUpdateCoordinator],
) -> None:
    """Actual HA platforms expose no URL sensor while image fetching still works."""
    entry, coordinator = runtime
    url = (
        "https://images.example.invalid/alarm?signature=PRIVATE_URL_MARKER" + "x" * 300
    )
    coordinator.data["CAM001"]["last_alarm_pic"] = url
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "CAM001_last_alarm_pic", config_entry=entry
    )
    added_sensors, added_images = MagicMock(), MagicMock()
    await sensor.async_setup_entry(hass, entry, added_sensors)
    await image.async_setup_entry(hass, entry, added_images)
    platforms = []
    jpeg = BytesIO()
    PILImage.new("RGB", (8, 8), (20, 80, 40)).save(jpeg, format="JPEG")
    try:
        for domain, additions in [("sensor", added_sensors), ("image", added_images)]:
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
            actual = [
                entity for call in additions.call_args_list for entity in call.args[0]
            ]
            await platform.async_add_entities(actual)
            platforms.append(platform)
            if domain == "image":
                last_motion = next(
                    entity
                    for entity in actual
                    if entity.unique_id == "CAM001_last_motion_image"
                )
                fetch = AsyncMock(
                    return_value=httpx.Response(200, content=jpeg.getvalue())
                )
                monkeypatch.setattr(last_motion, "_fetch_url", fetch)
                result = await last_motion.async_image()
                assert result is not None
                assert PILImage.open(BytesIO(result)).size == (8, 8)
                fetch.assert_awaited_once_with(url)
                assert {entity.unique_id for entity in actual} == {
                    "CAM001_last_motion_image",
                    "CAM001_snapshot",
                }
        await hass.async_block_till_done()
        assert registry.async_get(old.entity_id) is None
        assert hass.states.get(old.entity_id) is None
        assert coordinator.data["CAM001"]["last_alarm_pic"] == url
        assert not any(
            "PRIVATE_URL_MARKER" in state.state
            or "PRIVATE_URL_MARKER" in str(state.attributes)
            for state in hass.states.async_all()
        )
    finally:
        for platform in platforms:
            await platform.async_reset()
