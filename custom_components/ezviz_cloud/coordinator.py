"""Provides the ezviz DataUpdateCoordinator."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import logging

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class EzvizDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching EZVIZ data."""

    def __init__(
        self, hass: HomeAssistant, *, api: EzvizClient, api_timeout: int
    ) -> None:
        """Initialize global EZVIZ data updater."""
        self.ezviz_client = api
        self._api_timeout = api_timeout
        self._poll_future: asyncio.Future | None = None
        update_interval = timedelta(seconds=30)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self) -> dict:
        """Fetch data from EZVIZ."""
        try:
            if self._poll_future is None or self._poll_future.done():
                self._poll_future = asyncio.ensure_future(
                    self.hass.async_add_executor_job(self.ezviz_client.load_cameras)
                )
                self._poll_future.add_done_callback(_retrieve_worker_error)
            async with asyncio.timeout(self._api_timeout):
                return deepcopy(await asyncio.shield(self._poll_future))

        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error

        except (InvalidURL, HTTPError, PyEzvizError, OSError, TimeoutError) as error:
            raise UpdateFailed(f"EZVIZ polling failed ({type(error).__name__})") from error

    def merge_mqtt_update(self, serial: str, mqtt_data: dict) -> None:
        """Merge MQTT update data into the coordinator."""

        # Push cannot invent inventory entries or mutate an earlier snapshot.
        if not self.data or serial not in self.data:
            return
        snapshot = deepcopy(self.data)
        ext = mqtt_data.get("ext")
        if not isinstance(ext, dict):
            return
        if ext.get("image"):
            snapshot[serial].update(
                last_alarm_type_code=ext.get("alert_type_code"),
                last_alarm_time=ext.get("time"),
                last_alarm_pic=ext.get("image"),
                last_alarm_type_name=mqtt_data.get("alert"),
                Motion_Trigger=True,
            )

        # Important: broadcast a *new* top-level dict so listeners update
        self.async_set_updated_data(snapshot)


def _retrieve_worker_error(future: asyncio.Future) -> None:
    """Retrieve late failures without cancelling a still-owned executor job."""
    if not future.cancelled():
        future.exception()
