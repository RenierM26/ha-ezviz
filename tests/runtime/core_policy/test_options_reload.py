"""Options completion with live cameras must leave reload ownership to HA."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import custom_components
from custom_components.ezviz_cloud import config_flow
from custom_components.ezviz_cloud.const import DOMAIN
import pytest

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.runtime.test_hardening import entities


@pytest.mark.parametrize("camera_options", [False, True])
@pytest.mark.parametrize("changed", [False, True])
async def test_options_save_with_attached_camera(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    camera_options: bool,
    changed: bool,
) -> None:
    """Real options manager saves with a live camera and reloads only on change."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = config_flow
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    entry, _api, coordinator, camera, *_ = entities(hass)
    original_camera = {
        "username": "admin",
        "password": "sticker",
        "enc_key": "key",
        "rtsp_uses_verification_code": False,
        "ffmpeg_arguments": "/old/path",
    }
    hass.config_entries.async_update_entry(
        entry, options={"timeout": 41, "cameras": {"CAM001": original_camera}}
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    await camera.async_added_to_hass()
    try:
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"],
            {"next_step_id": "camera_select" if camera_options else "cloud"},
        )
        if camera_options:
            result = await manager.async_configure(
                result["flow_id"], {"serial": "CAM001"}
            )
            payload = {
                **original_camera,
                "ffmpeg_arguments": "/new/path" if changed else "/old/path",
                "ephemeral_test_rtsp": False,
            }
        else:
            payload = {"timeout": 42 if changed else 41}
        result = await manager.async_configure(result["flow_id"], payload)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert not entry.update_listeners
        if changed:
            reload_entry.assert_awaited_once_with(entry.entry_id)
        else:
            reload_entry.assert_not_awaited()
        assert entry.options["timeout"] == (
            42 if changed and not camera_options else 41
        )
        assert entry.options["cameras"]["CAM001"]["ffmpeg_arguments"] == (
            "/new/path" if changed and camera_options else "/old/path"
        )
    finally:
        await camera.async_will_remove_from_hass()
        await coordinator.async_close()
