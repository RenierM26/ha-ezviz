"""Support for EZVIZ Switch sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EzvizSwitchEntityDescription(SwitchEntityDescription):
    """Describe a EZVIZ switch."""

    supported_ext: str | None
    method: Callable[[EzvizClient, str, int], Any]
    switch_state: Callable[[dict], Any]


SWITCH_TYPES: dict[int | str, EzvizSwitchEntityDescription] = {
    1: EzvizSwitchEntityDescription(
        key="ALARM_TONE",
        translation_key="voice_prompt",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportAlarmVoice.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 1, enable
        ),
        switch_state=lambda data: data["switches"].get(1),
    ),
    3: EzvizSwitchEntityDescription(
        key="LIGHT",
        translation_key="status_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=None,
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 3, enable
        ),
        switch_state=lambda data: data["switches"].get(3),
    ),
    7: EzvizSwitchEntityDescription(
        key="PRIVACY",
        translation_key="privacy",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportPtzPrivacy.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 7, enable
        ),
        switch_state=lambda data: data["switches"].get(7),
    ),
    10: EzvizSwitchEntityDescription(
        key="INFRARED_LIGHT",
        translation_key="infrared_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportCloseInfraredLight.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 10, enable
        ),
        switch_state=lambda data: data["switches"].get(10),
    ),
    21: EzvizSwitchEntityDescription(
        key="SLEEP",
        translation_key="sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportSleep.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 21, enable
        ),
        switch_state=lambda data: data["switches"].get(21),
    ),
    22: EzvizSwitchEntityDescription(
        key="SOUND",
        translation_key="audio",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportAudioOnoff.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 22, enable
        ),
        switch_state=lambda data: data["switches"].get(22),
    ),
    25: EzvizSwitchEntityDescription(
        key="MOBILE_TRACKING",
        translation_key="motion_tracking",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportIntelligentTrack.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 25, enable
        ),
        switch_state=lambda data: data["switches"].get(25),
    ),
    29: EzvizSwitchEntityDescription(
        key="ALL_DAY_VIDEO",
        translation_key="all_day_video_recording",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportFulldayRecord.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 29, enable
        ),
        switch_state=lambda data: data["switches"].get(29),
    ),
    32: EzvizSwitchEntityDescription(
        key="AUTO_SLEEP",
        translation_key="auto_sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportAutoSleep.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 32, enable
        ),
        switch_state=lambda data: data["switches"].get(32),
    ),
    301: EzvizSwitchEntityDescription(
        key="LIGHT_FLICKER",
        translation_key="flicker_light_on_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportActiveDefense.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 301, enable
        ),
        switch_state=lambda data: data["switches"].get(301),
    ),
    305: EzvizSwitchEntityDescription(
        key="ALARM_LIGHT_RELEVANCE",
        translation_key="pir_motion_activated_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportLightRelate.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 305, enable
        ),
        switch_state=lambda data: data["switches"].get(305),
    ),
    306: EzvizSwitchEntityDescription(
        key="TAMPER_ALARM",
        translation_key="tamper_alarm",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportTamperAlarm.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 306, enable
        ),
        switch_state=lambda data: data["switches"].get(306),
    ),
    650: EzvizSwitchEntityDescription(
        key="TRACKING",
        translation_key="follow_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext=str(SupportExt.SupportTracking.value),
        method=lambda ezviz_client, serial, enable: ezviz_client.switch_status(
            serial, 650, enable
        ),
        switch_state=lambda data: data["switches"].get(650),
    ),
    "encrypted": EzvizSwitchEntityDescription(
        key="encrypted",
        translation_key="encrypted",
        supported_ext=str(SupportExt.SupportEncrypt.value),
        method=lambda pyezviz_client, serial, enable: pyezviz_client.set_video_enc(
            serial, enable
        ),
        switch_state=lambda data: data["encrypted"],
    ),
    "push_notify_alarm": EzvizSwitchEntityDescription(
        key="push_notify_alarm",
        translation_key="push_notify_alarm",
        supported_ext=None,
        method=lambda pyezviz_client, serial, enable: pyezviz_client.do_not_disturb(
            serial, enable ^ 1
        ),
        switch_state=lambda data: data["push_notify_alarm"],
    ),
    "push_notify_call": EzvizSwitchEntityDescription(
        key="push_notify_call",
        translation_key="push_notify_call",
        supported_ext=str(SupportExt.SupportAlarmVoice.value),
        method=lambda pyezviz_client, serial, enable: pyezviz_client.set_answer_call(
            serial, enable ^ 1
        ),
        switch_state=lambda data: data["push_notify_call"],
    ),
    "offline_notify": EzvizSwitchEntityDescription(
        key="offline_notify",
        translation_key="offline_notify",
        supported_ext=str(SupportExt.SupportAlarmVoice.value),
        method=lambda pyezviz_client,
        serial,
        enable: pyezviz_client.set_offline_notification(serial, enable),
        switch_state=lambda data: data["offline_notify"],
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ switch based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities_to_add = [
        EzvizSwitch(coordinator, camera, switch_number)
        for camera in coordinator.data
        for switch_number in coordinator.data[camera]["switches"]
        if switch_number in SWITCH_TYPES
        if SWITCH_TYPES[switch_number].supported_ext
        in coordinator.data[camera]["supportExt"]
        or SWITCH_TYPES[switch_number].supported_ext is None
    ]

    entities_to_add.extend(
        EzvizSwitch(coordinator, camera, switch)
        for camera in coordinator.data
        for switch in coordinator.data[camera]
        if switch in SWITCH_TYPES
        if SWITCH_TYPES[switch].supported_ext in coordinator.data[camera]["supportExt"]
        or SWITCH_TYPES[switch].supported_ext is None
    )

    async_add_entities(entities_to_add)


class EzvizSwitch(EzvizEntity, SwitchEntity):
    """Representation of a EZVIZ sensor."""

    entity_description: EzvizSwitchEntityDescription

    def __init__(
        self, coordinator: EzvizDataUpdateCoordinator, serial: str, switch: int | str
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial)
        self.entity_description = SWITCH_TYPES[switch]
        self._attr_unique_id = (
            f"{serial}_{self._camera_name}.{self.entity_description.key}"
        )
        self._attr_is_on = self.entity_description.switch_state(self.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Change a device switch on the camera."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.method,
                self.coordinator.ezviz_client,
                self._serial,
                1,
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Failed to turn on switch {self.name}") from err

        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Change a device switch on the camera."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.method,
                self.coordinator.ezviz_client,
                self._serial,
                0,
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Failed to turn on switch {self.name}") from err

        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = self.entity_description.switch_state(self.data)
        super()._handle_coordinator_update()
