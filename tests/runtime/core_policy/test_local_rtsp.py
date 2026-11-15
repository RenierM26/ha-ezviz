"""Local RTSP entity setup, real HA services and coordinator polling lifecycle."""

from datetime import timedelta
import logging
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import custom_components
from custom_components.ezviz_cloud import local_rtsp, switch
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import LocalRtspCoordinator
from custom_components.ezviz_cloud.diagnostics import async_get_config_entry_diagnostics
from custom_components.ezviz_cloud.runtime import RuntimeCleanup
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.local.hcnetsdk.api import EzvizLanServicesSwitchState
import pytest

from homeassistant import loader
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.switch.const import DATA_COMPONENT
from homeassistant.components.switch.services import async_setup_services
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

from tests.runtime.test_hardening import entities

CONFIG = {"host": "192.0.2.10", "username": "admin", "password": "LAN_SECRET_MARKER"}


def response(enabled: int) -> EzvizLanServicesSwitchState:
    """Use detached observations, not an SDK optimistic setter cache."""
    return EzvizLanServicesSwitchState(
        raw={"servicesSwitch": {"rtsp": enabled, "web": 0}}
    )


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("category", ["IPC", "BatteryCamera"])
@pytest.mark.parametrize("reachable", [False, True])
async def test_only_opted_nonbattery_camera_gets_local_switch(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    category: str,
    reachable: bool,
) -> None:
    """A failed LAN initial read cannot prevent other cloud switches loading."""
    entry, _api, account, *_ = entities(hass)
    account.data["CAM001"]["device_category"] = category
    account.data["CAM001"]["switches"] = {1: 0}
    entry.runtime_data = SimpleNamespace(coordinator=account)
    hass.config_entries.async_update_entry(
        entry,
        options={"cameras": {"CAM001": {"local_rtsp": CONFIG if configured else {}}}},
    )
    client = MagicMock()
    client.services_switch_state.return_value = response(0)
    if not reachable:
        client.services_switch_state.side_effect = OSError("synthetic")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(local_rtsp, "HcNetSdkPurePythonClient", factory)
    monkeypatch.setattr(switch, "migrate_unique_ids_with_coordinator", AsyncMock())
    added: list[SwitchEntity] = []
    try:
        await switch.async_setup_entry(hass, entry, added.extend)
        local = [item for item in added if isinstance(item, switch.EzvizRtspSwitch)]
        expected = configured and category == "IPC"
        assert len(local) == int(expected)
        assert factory.call_count == int(expected)
        if local:
            assert local[0].available is reachable
            await local[0].coordinator.async_shutdown()
        assert account.last_update_success
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_registered_switch_services_polling_rename_and_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-back controls real HA state; listeners alone own interval polling."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    entry, _api, account, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=account)
    local = LocalRtspCoordinator(
        account, "CAM001", local_rtsp.LocalRtspConfig(**CONFIG)
    )
    physical = False
    threads: list[int] = []
    client = MagicMock()

    def read() -> EzvizLanServicesSwitchState:
        threads.append(get_ident())
        return response(int(physical))

    def write(_payload: object, *, rtsp: int) -> SimpleNamespace:
        nonlocal physical
        physical = bool(rtsp)
        return SimpleNamespace(succeeded=True, json=lambda: {"statusCode": 1})

    client.services_switch_state.side_effect = read
    client.services_switch_set.side_effect = write
    monkeypatch.setattr(
        local_rtsp, "HcNetSdkPurePythonClient", lambda *_args, **_kwargs: client
    )
    account.async_request_refresh = AsyncMock()
    local.last_update_success = False
    await local.async_refresh()
    component_owner = EntityComponent[SwitchEntity](
        logging.getLogger(__name__), "switch", hass
    )
    hass.data[DATA_COMPONENT] = component_owner
    async_setup_services(hass)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="switch",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    entity = switch.EzvizRtspSwitch(local)
    try:
        await platform.platform_data.async_load_translations()
        await platform.async_add_entities([entity])
        assert entity.name == "RTSP service"
        assert hass.states.get(entity.entity_id).state == "off"
        assert local._unsub_refresh is not None
        assert local.update_interval == timedelta(minutes=5)
        registry = er.async_get(hass)
        registered = registry.async_get(entity.entity_id)
        assert registered is not None and registered.device_id
        for service, expected in (("turn_on", "on"), ("turn_off", "off")):
            await hass.services.async_call(
                "switch", service, {"entity_id": entity.entity_id}, blocking=True
            )
            assert hass.states.get(entity.entity_id).state == expected
        assert all(thread != get_ident() for thread in threads)
        account.async_request_refresh.assert_not_awaited()
        if hasattr(entity, "async_entity_id_changed"):
            registry.async_update_entity(
                entity.entity_id, new_entity_id="switch.renamed_rtsp"
            )
            await hass.async_block_till_done()
            assert hass.states.get("switch.renamed_rtsp").state == "off"
        # The device independently disables/enables RTSP: local polling, not cloud,
        # must publish it through the same listener.
        physical = True
        await local.async_refresh()
        assert hass.states.get(entity.entity_id).state == "on"
        client.services_switch_state.side_effect = PyEzvizError("LAN_SECRET_MARKER")
        await local.async_refresh()
        assert hass.states.get(entity.entity_id).state == "unavailable"
        await platform.async_reset()
        assert local._unsub_refresh is None
    finally:
        await platform.async_reset()
        await local.async_shutdown()
        await account.async_close()


@pytest.mark.parametrize(
    "options", [True, {"host": "localhost"}, {**CONFIG, "password": ""}]
)
async def test_invalid_saved_lan_options_do_not_fail_cloud_platform(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, options: object
) -> None:
    """Malformed optional LAN configuration must not prevent cloud setup."""
    entry, _api, account, *_ = entities(hass)
    entry.runtime_data = SimpleNamespace(coordinator=account)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"local_rtsp": options}}}
    )
    factory = MagicMock()
    monkeypatch.setattr(local_rtsp, "HcNetSdkPurePythonClient", factory)
    monkeypatch.setattr(switch, "migrate_unique_ids_with_coordinator", AsyncMock())
    added: list[SwitchEntity] = []
    try:
        await switch.async_setup_entry(hass, entry, added.extend)
        assert not any(isinstance(item, switch.EzvizRtspSwitch) for item in added)
        factory.assert_not_called()
        assert account.last_update_success
    finally:
        await account.async_close()


async def test_lan_credentials_and_address_never_enter_diagnostics(
    hass: HomeAssistant,
) -> None:
    """The diagnostic allowlist excludes the entire optional LAN configuration."""
    entry, _api, account, *_ = entities(hass)
    entry.runtime_data = RuntimeCleanup(hass, account)
    hass.config_entries.async_update_entry(
        entry, options={"cameras": {"CAM001": {"local_rtsp": CONFIG}}}
    )
    try:
        report = await async_get_config_entry_diagnostics(hass, entry)
        assert CONFIG["password"] not in str(report)
        assert CONFIG["host"] not in str(report)
    finally:
        await account.async_close()
