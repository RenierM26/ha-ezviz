"""Register camera actions independently of account setup."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import DOMAIN, SERVICE_CAPTURE_PICTURE, SERVICE_WAKE_DEVICE


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register wake and capture against this integration's camera platforms."""
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_WAKE_DEVICE,
        entity_domain=Platform.CAMERA,
        schema=None,
        func="async_perform_wake_device",
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CAPTURE_PICTURE,
        entity_domain=Platform.CAMERA,
        schema=None,
        func="async_capture_picture",
        supports_response=SupportsResponse.OPTIONAL,
    )
