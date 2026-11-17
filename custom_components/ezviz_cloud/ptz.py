"""Native HA input validation and translation of SDK-owned PTZ routing errors."""

from typing import Any

import probatio
from pyezvizapi.devices.channels import CameraChannelError, point_ptz_route

from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN


def coordinate(value: object) -> float:
    """Require real normalized input, never bool, NaN or coerced text."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise probatio.Invalid("Coordinates must be numbers between 0 and 1")
    return float(value)


def point_route(
    data: dict[str, Any], lens: str = "auto", *, channel_key: str | None = None
) -> tuple[str, str]:
    """Let the SDK select the protocol route; expose actionable HA errors."""
    try:
        return point_ptz_route(data, lens, channel_key=channel_key)
    except CameraChannelError as error:
        key = {
            "unsupported": "ptz_point_unsupported",
            "selection_required": "ptz_lens_required",
            "unavailable": "ptz_lens_unavailable",
        }.get(error.reason, "ptz_route_unknown")
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"lens": lens},
        ) from error
