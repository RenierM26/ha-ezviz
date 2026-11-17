"""Register camera actions independently of account setup."""

import probatio

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import (
    DOMAIN,
    SERVICE_CAPTURE_PICTURE,
    SERVICE_PTZ_CONTROL_COORDINATES,
    SERVICE_WAKE_DEVICE,
)
from .ptz import coordinate


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register camera actions independently of account setup/availability."""
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

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_PTZ_CONTROL_COORDINATES,
        entity_domain=Platform.CAMERA,
        schema={
            probatio.Required("x_axis"): coordinate,
            probatio.Required("y_axis"): coordinate,
            probatio.Optional("lens", default="auto"): probatio.In(("auto", "1", "2")),
        },
        func="async_ptz_control_coordinates",
    )
