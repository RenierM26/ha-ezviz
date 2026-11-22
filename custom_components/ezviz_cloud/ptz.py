"""Native HA input validation and translation of SDK-owned PTZ routing errors."""

from typing import Any

import probatio
from pyezvizapi.devices.capabilities import SupportExtView
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
    data: dict[str, Any],
    lens: str = "auto",
    *,
    channel_key: str | None = None,
    bind_primary: bool = False,
) -> tuple[str, str]:
    """Let the SDK select the protocol route; expose actionable HA errors."""
    if bind_primary:
        # Preserve the SDK's fixed direct 586 route, including numeric strings.
        flag = SupportExtView.from_camera_data(data).get("586")
        if isinstance(flag, str):
            try:
                flag = int(flag)
            except ValueError:
                flag = None
        if type(flag) is not int or flag != 1:
            lens = "1"
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
