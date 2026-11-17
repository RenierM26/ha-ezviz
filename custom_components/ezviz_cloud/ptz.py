"""Validate app-backed point PTZ coordinates, capabilities and resource routes."""

from collections.abc import Mapping
import re
from typing import Any

import probatio
from pyezvizapi.devices.capabilities import SupportExtView

from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN

_PART = re.compile(r"[A-Za-z0-9_]{1,64}")
_VIDEO_RESOURCE = re.compile(r"Video(?:_[0-9]+)?")
DIRECT_ROUTE = ("Video_1", "1")


def coordinate(value: object) -> float:
    """Require real normalized input, never bool, NaN or coerced text."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise probatio.Invalid("Coordinates must be numbers between 0 and 1")
    return float(value)


def route_part(value: object) -> str:
    """Keep explicit resource/index values within one URL path segment."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        value = str(value)
    if not isinstance(value, str) or not _PART.fullmatch(value):
        raise probatio.Invalid("Resource and index must be nonempty path identifiers")
    return value


def _enabled(capabilities: SupportExtView, flag: str) -> bool:
    value = capabilities.get(flag)
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return False
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def point_route(
    data: dict[str, Any], resource_identifier: str | None, local_index: str | None
) -> tuple[str, str]:
    """Use direct-tap defaults or a uniquely observed selected-video resource."""
    capabilities = SupportExtView.from_camera_data(data)
    direct, locate = _enabled(capabilities, "586"), _enabled(capabilities, "724")
    if not direct and not locate:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ptz_point_unsupported"
        )
    if (resource_identifier is None) != (local_index is None):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ptz_route_pair_required"
        )
    requested = (
        (resource_identifier, local_index)
        if resource_identifier is not None and local_index is not None
        else None
    )
    if direct and requested in (None, DIRECT_ROUTE):
        return DIRECT_ROUTE
    if not locate:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ptz_point_unsupported"
        )
    routes: set[tuple[str, str]] = set()
    rows = data.get("resourceInfos")
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, Mapping):
            continue
        resource = row.get("resourceIdentifier")
        if not isinstance(resource, str) or not _VIDEO_RESOURCE.fullmatch(resource):
            continue
        try:
            index = route_part(row.get("localIndex"))
        except probatio.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ptz_route_unknown"
            ) from err
        routes.add((resource, index))
    if requested is not None and requested in routes:
        return requested
    if requested is None and len(routes) == 1:
        return next(iter(routes))
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="ptz_route_unknown"
    )
