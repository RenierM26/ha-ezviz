"""Support for Ezviz alarm."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from pyezviz import PyEzvizError
from pyezviz.constants import DefenseModeType

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityDescription,
    AlarmControlPanelEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_DISARMED,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_COORDINATOR,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)
PARALLEL_UPDATES = 0


@dataclass
class EzvizAlarmControlPanelEntityDescriptionMixin:
    """Mixin values for EZVIZ Alarm control panel entities."""

    ezviz_alarm_states: list


@dataclass
class EzvizAlarmControlPanelEntityDescription(
    AlarmControlPanelEntityDescription, EzvizAlarmControlPanelEntityDescriptionMixin
):
    """Describe an EZVIZ Alarm control panel entity."""


ALARM_TYPE = EzvizAlarmControlPanelEntityDescription(
    key="ezviz_alarm",
    ezviz_alarm_states=[
        None,
        STATE_ALARM_DISARMED,
        STATE_ALARM_ARMED_AWAY,
        STATE_ALARM_ARMED_HOME,
    ],
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Ezviz alarm control panel."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities([EzvizAlarm(coordinator, entry.entry_id, ALARM_TYPE)])


class EzvizAlarm(CoordinatorEntity, AlarmControlPanelEntity):
    """Representation of an Ezviz alarm control panel."""

    coordinator: EzvizDataUpdateCoordinator
    entity_description: EzvizAlarmControlPanelEntityDescription
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )
    _attr_code_arm_required = False

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        entry_id: str,
        entity_description: EzvizAlarmControlPanelEntityDescription,
    ) -> None:
        """Initialize alarm control panel entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{entity_description.key}"
        self.entity_description = entity_description
        self._attr_device_info: DeviceInfo = {
            "identifiers": {(DOMAIN, "EZVIZ Alarm")},
            "name": "EZVIZ Alarm",
            "model": "EZVIZ Alarm",
            "manufacturer": MANUFACTURER,
        }
        self._attr_state = None

    async def async_added_to_hass(self) -> None:
        """Entity added to hass."""
        self.hass.async_add_executor_job(self.update)

    def alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        try:
            if self.coordinator.ezviz_client.api_set_defence_mode(
                DefenseModeType.HOME_MODE.value
            ):
                self._attr_state = STATE_ALARM_DISARMED
                self.async_write_ha_state()

        except PyEzvizError as err:
            raise HomeAssistantError("Cannot disarm EZVIZ alarm") from err

    def alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        try:
            if self.coordinator.ezviz_client.api_set_defence_mode(
                DefenseModeType.AWAY_MODE.value
            ):
                self._attr_state = STATE_ALARM_ARMED_AWAY
                self.async_write_ha_state()

        except PyEzvizError as err:
            raise HomeAssistantError("Cannot arm EZVIZ alarm") from err

    def alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        try:
            if self.coordinator.ezviz_client.api_set_defence_mode(
                DefenseModeType.SLEEP_MODE.value
            ):
                self._attr_state = STATE_ALARM_ARMED_HOME
                self.async_write_ha_state()

        except PyEzvizError as err:
            raise HomeAssistantError("Cannot arm EZVIZ alarm") from err

    def update(self) -> None:
        """Fetch data from EZVIZ."""
        _LOGGER.debug("Updating %s", self.name)
        ezviz_alarm_state_number: str = "0"
        try:
            ezviz_alarm_state_number = (
                self.coordinator.ezviz_client.get_group_defence_mode()
            )
            _LOGGER.debug(ezviz_alarm_state_number)
            self._attr_state = self.entity_description.ezviz_alarm_states[
                int(ezviz_alarm_state_number)
            ]

            self.async_write_ha_state()

        except PyEzvizError as error:
            raise HomeAssistantError(
                f"Could not fetch EZVIZ alarm status: {error}"
            ) from error
