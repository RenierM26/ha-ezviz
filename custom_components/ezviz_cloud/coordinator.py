"""Provides the ezviz DataUpdateCoordinator."""

import asyncio
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
        self, hass: HomeAssistant, *, api: EzvizClient, api_timeout: int,
        # MAC address management
        use_ezvizapi_mac: bool
    ) -> None:
        """Initialize global EZVIZ data updater."""
        self.ezviz_client = api
        self._api_timeout = api_timeout
        # MAC address management
        self.use_ezvizapi_mac = use_ezvizapi_mac
        update_interval = timedelta(seconds=30)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    # async def _async_update_data(self) -> dict:
    #     """Fetch data from EZVIZ."""
    #     try:
    #         async with asyncio.timeout(self._api_timeout):
    #             return await self.hass.async_add_executor_job(
    #                 self.ezviz_client.load_cameras
    #             )

    #     except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
    #         raise ConfigEntryAuthFailed from error

    #     except (InvalidURL, HTTPError, PyEzvizError) as error:
    #         raise UpdateFailed(f"Invalid response from API: {error}") from error

    async def _async_update_data(self) -> dict:
        """Fetch data from EZVIZ and ensure MAC addresses did not come from pyezvizapi."""
        try:
            async with asyncio.timeout(self._api_timeout):
                raw_data = await self.hass.async_add_executor_job(
                    self.ezviz_client.load_cameras
                )
            # --- MAC address overloading EMERIC ---
            _LOGGER.warning("self.use_ezvizapi_mac: %s", self.use_ezvizapi_mac)
            for serial, device_data in raw_data.items():
                if self.use_ezvizapi_mac:
                    _LOGGER.warning("serial: %s   using MAC address provided by ezviz API: %s",serial,device_data.get("mac_address"))
                else:
                    # Generates a fake MAC address based on serial number when MAC address is not defined out of pyezvizapi
                    mac = f"65:63:3A:{serial[-2:]}:{serial[-4:-2]}:{serial[-6:-4]}"
                    _LOGGER.warning("serial: %s   creating a fake MAC address: %s",serial,mac)
                    device_data["mac_address"] = mac
            return raw_data
        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as error:
            raise ConfigEntryAuthFailed from error
        except (InvalidURL, HTTPError, PyEzvizError) as error:
            raise UpdateFailed(f"Invalid response from API: {error}") from error

    def merge_mqtt_update(self, serial: str, mqtt_data: dict) -> None:
        """Merge MQTT update data into the coordinator."""

        # Make sure coordinator has a dict for this device
        if serial not in self.data:
            self.data[serial] = {}

        # Update Image entity and corresponding sensor attibutes
        ext = mqtt_data["ext"]
        if ext.get("image"):
            self.data[serial].update(
                last_alarm_type_code=ext.get("alert_type_code"),
                last_alarm_time=ext.get("time"),
                last_alarm_pic=ext.get("image"),
                last_alarm_type_name=mqtt_data.get("alert"),
                Motion_Trigger=True,
            )

        # Important: broadcast a *new* top-level dict so listeners update
        self.async_set_updated_data(dict(self.data))