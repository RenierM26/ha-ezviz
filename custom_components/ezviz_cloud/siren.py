"""Support for EZVIZ sirens."""

from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, override

from pyezvizapi import HTTPError, PyEzvizError, SupportExt

from homeassistant.components.siren import (
    SirenEntity,
    SirenEntityDescription,
    SirenEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import homeassistant.helpers.event as evt
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity
from .errors import request_error
from .runtime import EzvizConfigEntry

PARALLEL_UPDATES = 1
OFF_DELAY = timedelta(seconds=60)  # Camera firmware has hard coded turn off.

SIREN_ENTITY_TYPE = SirenEntityDescription(
    key="siren",
    translation_key="siren",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    async_add_entities(
        EzvizSirenEntity(coordinator, camera, SIREN_ENTITY_TYPE)
        for camera in coordinator.data
        for capability, value in coordinator.data[camera]["supportExt"].items()
        if capability == str(SupportExt.SupportActiveDefense.value)
        if value != "0"
    )


class EzvizSirenEntity(EzvizBaseEntity, SirenEntity, RestoreEntity):
    """Representation of a EZVIZ Siren entity."""

    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: SirenEntityDescription,
    ) -> None:
        """Initialize the Siren."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.entity_description = description
        self._attr_is_on = False
        self._delay_listener: Callable[[], None] | None = None
        self._timer_generation = 0
        self._off_deadline: float | None = None
        self._removed = False
        self._removal_generation = 0

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        self._removed = False
        # Registry rename re-adds this object without a restore record at the
        # new ID. Resume its existing expiry, not another full firmware minute.
        if self._attr_is_on and self._off_deadline is not None:
            self._schedule_off_delay(reset=False)
            return
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON

        if self._attr_is_on:
            self._schedule_off_delay()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off camera siren."""
        generation = self._removal_generation
        try:
            result = await self.coordinator.async_execute(
                self.coordinator.ezviz_client.sound_alarm, self._serial, 1
            )

        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Turn off siren", err) from err

        if result and not self._removed and generation == self._removal_generation:
            self._cancel_off_delay()

            self._attr_is_on = False
            self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on camera siren."""
        generation = self._removal_generation
        try:
            result = await self.coordinator.async_execute(
                self.coordinator.ezviz_client.sound_alarm, self._serial, 2
            )

        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Turn on siren", err) from err

        if result and not self._removed and generation == self._removal_generation:
            self._attr_is_on = True
            self._schedule_off_delay()
            self.async_write_ha_state()

    @callback
    def _cancel_off_delay(self, *, clear_deadline: bool = True) -> None:
        """Invalidate even an already queued callback before cancelling the timer."""
        if clear_deadline:
            self._off_deadline = None
        self._timer_generation += 1
        if self._delay_listener is not None:
            self._delay_listener()
            self._delay_listener = None

    @callback
    def _schedule_off_delay(self, *, reset: bool = True) -> None:
        """Give each activation its own cancellable, generation-checked timer."""
        self._cancel_off_delay(clear_deadline=False)
        now = monotonic()
        if reset or self._off_deadline is None:
            self._off_deadline = now + OFF_DELAY.total_seconds()
        remaining = self._off_deadline - now
        if remaining <= 0:
            self._off_deadline = None
            self._attr_is_on = False
            return
        generation = self._timer_generation

        @callback
        def expire(now: datetime) -> None:
            if not self._removed and generation == self._timer_generation:
                self.off_delay_listener(now)

        self._delay_listener = evt.async_call_later(self.hass, remaining, expire)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Prevent delayed callbacks or pending commands writing after removal."""
        self._removed = True
        self._removal_generation += 1
        self._cancel_off_delay(clear_deadline=False)
        await super().async_will_remove_from_hass()

    @callback
    def off_delay_listener(self, now: datetime) -> None:
        """Switch device off after a delay.

        Camera firmware has hard coded turn off after 60 seconds.
        """
        self._attr_is_on = False
        self._off_deadline = None
        self._delay_listener = None
        self.async_write_ha_state()
