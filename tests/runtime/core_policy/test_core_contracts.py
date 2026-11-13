"""Core-style entity, action and form contracts on supported and dev HA."""

from datetime import timedelta
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import image, light
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from custom_components.ezviz_cloud.tokens import AcknowledgedStore
import pytest

from homeassistant import loader
from homeassistant.components.light import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.test_hardening import entities, entry_for


@pytest.fixture(autouse=True)
def integration(hass: HomeAssistant) -> None:
    """Resolve the integration without network, platforms or account setup."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component


async def test_camera_light_exposes_active_brightness_mode(hass: HomeAssistant) -> None:
    """Supported modes and active mode must agree before the first update."""
    _, _, coordinator, _, _, _ = entities(hass)
    entity = light.EzvizLight(coordinator, "CAM001", light.LIGHTS[0])
    assert entity.color_mode is ColorMode.BRIGHTNESS
    assert entity.supported_color_modes == {ColorMode.BRIGHTNESS}
    await coordinator.async_close()


async def test_images_are_added_as_one_flat_group(hass: HomeAssistant) -> None:
    """Multiple lenses and the snapshot remain concrete entities, not generators."""
    entry, _, coordinator, _, _, _ = entities(hass)
    coordinator.data["CAM001"]["supported_channels"] = 3
    # The platform only needs the coordinator, not background push/cleanup work.

    entry.runtime_data = RuntimeCleanup(hass, coordinator)
    added = MagicMock()
    await image.async_setup_entry(hass, entry, added)
    added.assert_called_once()
    actual = list(added.call_args.args[0])
    assert len(actual) == 4
    assert all(isinstance(item, Entity) for item in actual)
    assert {item.unique_id for item in actual} == {
        "CAM001_last_motion_image",
        "CAM001_last_motion_image_2",
        "CAM001_last_motion_image_3",
        "CAM001_snapshot",
    }
    await coordinator.async_close()


class ActionTarget(Entity):
    """A synthetic entity for the real platform action dispatcher."""

    _attr_should_poll = False

    def __init__(self, suffix: str) -> None:
        """Give each account target an independent identity and call history."""
        self._attr_unique_id = suffix
        self._attr_name = suffix
        self.calls: list[str] = []

    async def async_perform_wake_device(self) -> None:
        """Record a wake without contacting any device."""
        self.calls.append("wake")

    async def async_capture_picture(self) -> dict[str, str]:
        """Return a deterministic response without an acquisition."""
        self.calls.append("capture")
        return {"result": "synthetic"}


async def test_actions_register_before_accounts_and_dispatch_across_platforms(
    hass: HomeAssistant,
) -> None:
    """Preserve targets and optional capture responses after moving registration."""
    assert await ezviz_cloud.async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, "wake_device")
    assert hass.services.has_service(DOMAIN, "capture_picture")
    first, second = ActionTarget("first"), ActionTarget("second")
    platforms = []
    for target in (first, second):
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="camera",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        platform.config_entry = entry_for(hass)
        await platform.async_add_entities([target])
        platforms.append(platform)
    try:
        await hass.services.async_call(
            DOMAIN, "wake_device", {"entity_id": first.entity_id}, blocking=True
        )
        assert first.calls == ["wake"]
        assert second.calls == []
        result = await hass.services.async_call(
            DOMAIN,
            "capture_picture",
            {"entity_id": [first.entity_id, second.entity_id]},
            blocking=True,
            return_response=True,
        )
        assert first.calls == ["wake", "capture"]
        assert second.calls == ["capture"]
        assert result == {
            first.entity_id: {"result": "synthetic"},
            second.entity_id: {"result": "synthetic"},
        }
        await hass.services.async_call(
            DOMAIN, "capture_picture", {"entity_id": second.entity_id}, blocking=True
        )
        assert second.calls == ["capture", "capture"]
    finally:
        for platform in platforms:
            await platform.async_reset()


def test_form_translation_and_store_initial_state(hass: HomeAssistant) -> None:
    """A new acknowledged Store and camera selector have explicit initial contracts."""
    root = Path(image.__file__).parent
    for path in (root / "strings.json", root / "translations/en.json"):
        assert json.loads(path.read_text())["options"]["step"]["camera_select"]["data"][
            "serial"
        ]
    assert "[%key:" not in (root / "translations/en.json").read_text()
    store = AcknowledgedStore(hass, 1, "fixture", private=True, atomic_writes=True)
    assert (
        store._write_acknowledged is False
    )  # Check the private durable-write acknowledgement invariant.
