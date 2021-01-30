"""Config flow for ezviz."""
import logging
from typing import Any, Dict, Optional

from pyezviz.client import EzvizClient, PyEzvizError
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_TIMEOUT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.typing import ConfigType, HomeAssistantType

from .const import (
    CONF_FFMPEG_ARGUMENTS,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_REGION,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def validate_input(hass: HomeAssistantType, data: dict) -> Dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    # constructor does login call
    EzvizClient(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_REGION],
        data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )


@config_entries.HANDLERS.register(DOMAIN)
class EzvizConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ezviz."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return EzvizOptionsFlowHandler(config_entry)

    async def async_step_import(
        self, user_input: Optional[ConfigType] = None
    ) -> Dict[str, Any]:
        """Handle a flow initiated by configuration file."""
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: Optional[ConfigType] = None
    ) -> Dict[str, Any]:
        """Handle a flow initiated by the user."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors = {}
        default_username = ""

        if user_input is not None:
            if CONF_TIMEOUT not in user_input:
                user_input[CONF_TIMEOUT] = DEFAULT_TIMEOUT

            default_username = user_input[CONF_USERNAME]

            try:
                await self.hass.async_add_executor_job(
                    validate_input, self.hass, user_input
                )
            except PyEzvizError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")
            else:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=user_input,
                )

        data_schema = {
            vol.Required(CONF_USERNAME, default=default_username): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_REGION, default=DEFAULT_REGION): str,
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(data_schema),
            errors=errors or {},
        )


class EzvizOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Canary client options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: Optional[ConfigType] = None):
        """Manage Ezviz options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = {
            vol.Optional(
                CONF_TIMEOUT,
                default=self.config_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            ): int,
            vol.Optional(
                CONF_FFMPEG_ARGUMENTS,
                default=self.config_entry.options.get(
                    CONF_FFMPEG_ARGUMENTS, DEFAULT_FFMPEG_ARGUMENTS
                ),
            ): str,
        }

        return self.async_show_form(step_id="init", data_schema=vol.Schema(options))
