"""Actionable failures without leaking vendor payloads or requesting reauth."""

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from unittest.mock import MagicMock

import custom_components
from custom_components.ezviz_cloud import (
    alarm_control_panel as alarm,
    button,
    light,
    number,
    select,
    siren,
    switch,
)
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.errors import error_category, request_error
from pyezvizapi.exceptions import HTTPError, PyEzvizError
import pytest
import requests

from homeassistant import loader
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import translation
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import UpdateFailed

from tests.runtime.test_hardening import entities


def http_failure(status: int) -> HTTPError:
    """Reproduce the published SDK's empty, chained HTTP error."""
    response = requests.Response()
    response.status_code = status
    response.url = "https://private.invalid/?token=SECRET_MARKER"
    response._content = b"SECRET_MARKER vendor payload"
    cause = requests.HTTPError("SECRET_MARKER", response=response)
    error = HTTPError()
    error.__cause__ = cause
    return error


@pytest.fixture(autouse=True)
async def translated_errors(hass: HomeAssistant) -> None:
    """Load actual exception translations as HA does during integration setup."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.config.components.add(DOMAIN)
    await translation.async_get_translations(hass, "en", "exceptions", {DOMAIN})


@pytest.mark.parametrize(
    ("failure", "key", "detail"),
    [
        (http_failure(403), "request_forbidden", "HTTP 403"),
        (http_failure(504), "request_timeout", "HTTP 504"),
        (http_failure(408), "request_timeout", "HTTP 408"),
        (http_failure(500), "request_http", "HTTP 500"),
        (http_failure(401), "request_http", "HTTP 401"),
        (
            PyEzvizError("Failed: response code=403, fields=2 SECRET_MARKER"),
            "request_forbidden",
            "API 403",
        ),
        (
            PyEzvizError("Failed: response code=2009, fields=2 SECRET_MARKER"),
            "request_api",
            "API 2009",
        ),
        (PyEzvizError("SECRET_MARKER 403"), "request_failed", "PyEzvizError"),
        (HTTPError(), "request_failed", "HTTPError"),
        (TimeoutError(), "request_timeout", "timeout"),
        (requests.Timeout("SECRET_MARKER"), "request_timeout", "timeout"),
        (OSError("SECRET_MARKER"), "request_connection", "OSError"),
    ],
)
async def test_error_details_are_safe_and_translated(
    hass: HomeAssistant, failure: Exception, key: str, detail: str
) -> None:
    """HTTP and API codes remain distinct; arbitrary text is never forwarded."""
    assert error_category(failure) == (key, detail)
    error = request_error("Test operation", failure)
    assert error.translation_key == key
    message = str(error)
    assert "Test operation failed:" in message
    assert detail in message
    assert "SECRET_MARKER" not in message
    assert "private.invalid" not in message
    assert not message.endswith(":")


async def test_alarm_timeout_recovers_without_reauth(hass: HomeAssistant) -> None:
    """A 504 preserves last known state; the next scheduled request recovers."""
    entry, api, coordinator, *_ = entities(hass)
    error = http_failure(504)
    api.get_group_defence_mode = MagicMock(side_effect=[error, "1"])
    entity = alarm.EzvizAlarm(
        coordinator, entry.entry_id, DeviceInfo(), alarm.ALARM_TYPE
    )
    entity.hass = hass
    entity._attr_alarm_state = AlarmControlPanelState.ARMED_HOME
    with pytest.raises(HomeAssistantError, match="HTTP 504") as caught:
        await entity.async_update()
    assert caught.value.__cause__ is error
    assert entity.alarm_state == AlarmControlPanelState.ARMED_HOME
    await entity.async_update()
    assert entity.alarm_state == AlarmControlPanelState.DISARMED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await coordinator.async_close()


@pytest.mark.parametrize("action", ["on", "off", "brightness"])
async def test_light_permission_denial_preserves_state(
    hass: HomeAssistant, action: str
) -> None:
    """A shared-account denial is visible without optimistic state changes."""
    _, api, coordinator, *_ = entities(hass)
    failure = http_failure(403)
    command = MagicMock(side_effect=failure)
    desc = replace(
        light.LIGHTS[0], power_on=command, power_off=command, set_brightness=command
    )
    entity = light.EzvizLight(coordinator, "CAM001", desc)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    if action == "off":
        call = entity.async_turn_off()
    else:
        call = entity.async_turn_on(
            **({"brightness": 128} if action == "brightness" else {})
        )
    with pytest.raises(
        HomeAssistantError, match="denied permission.*HTTP 403"
    ) as caught:
        await call
    assert "Family & Guests" in str(caught.value)
    assert caught.value.__cause__ is failure
    assert not entity.is_on
    entity.async_write_ha_state.assert_not_called()
    assert command.call_count == 1
    api.close_session.assert_not_called()
    await coordinator.async_close()


@pytest.mark.parametrize(
    "kind",
    [
        "button",
        "number",
        "select",
        "switch_on",
        "switch_off",
        "siren_on",
        "siren_off",
        "wake",
        "capture",
        "arm",
        "disarm",
    ],
)
async def test_other_adapters_surface_safe_permission_errors(
    hass: HomeAssistant, kind: str
) -> None:
    """All camera action families preserve the SDK cause and permission code."""
    entry, api, coordinator, camera, *_ = entities(hass)
    command = MagicMock(
        side_effect=PyEzvizError("Denied: response code=403, fields=2 SECRET_MARKER")
    )
    call: Callable
    if kind == "button":
        entity = button.EzvizButtonEntity(
            coordinator, "CAM001", replace(button.BUTTON_ENTITIES[0], method=command)
        )
        call = entity.async_press
    elif kind == "number":
        entity = number.EzvizNumber(
            coordinator,
            "CAM001",
            replace(number.STATIC_NUMBER_DESCRIPTIONS[0], set_value=command),
        )
        call = partial(entity.async_set_native_value, 1)
    elif kind == "select":
        entity = select.EzvizSelect(
            coordinator,
            "CAM001",
            replace(select.SELECTS[0], set_current_option=command),
        )
        call = partial(entity.async_select_option, entity.entity_description.options[0])
    elif kind.startswith("switch"):
        entity = switch.EzvizSwitch(
            coordinator,
            "CAM001",
            replace(switch.SWITCHES[0], method=command),
        )
        call = entity.async_turn_on if kind.endswith("on") else entity.async_turn_off
    elif kind.startswith("siren"):
        api.sound_alarm = command
        entity = siren.EzvizSirenEntity(coordinator, "CAM001", siren.SIREN_ENTITY_TYPE)
        call = entity.async_turn_on if kind.endswith("on") else entity.async_turn_off
    elif kind in ("wake", "capture"):
        api.get_detection_sensibility = api.capture_picture = command
        call = (
            camera.async_perform_wake_device
            if kind == "wake"
            else camera.async_capture_picture
        )
    else:
        api.api_set_defence_mode = command
        entity = alarm.EzvizAlarm(
            coordinator, entry.entry_id, DeviceInfo(), alarm.ALARM_TYPE
        )
        call = (
            entity.async_alarm_arm_home if kind == "arm" else entity.async_alarm_disarm
        )
    with pytest.raises(
        HomeAssistantError, match="denied permission.*API 403"
    ) as caught:
        await call()
    assert caught.value.__cause__ is command.side_effect
    assert "SECRET_MARKER" not in str(caught.value)
    await coordinator.async_close()


async def test_poll_timeout_has_safe_detail_and_no_reauth(hass: HomeAssistant) -> None:
    """Main polling surfaces a transport failure rather than an auth failure."""
    _, api, coordinator, *_ = entities(hass)
    api.poll_devices = MagicMock(side_effect=http_failure(504))
    with pytest.raises(UpdateFailed, match="Poll devices failed:.*HTTP 504"):
        await (
            coordinator.async_refresh()
        ) if False else await coordinator._async_update_data()
    await coordinator.async_close()


def test_malformed_cause_cycle_is_bounded() -> None:
    """Unknown/cyclic chains do not hang or leak private exception contents."""
    error = PyEzvizError("SECRET_MARKER")
    error.__cause__ = error
    assert error_category(error) == ("request_failed", "PyEzvizError")
