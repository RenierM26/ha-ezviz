"""Pure input validation shared by the account and options flows."""

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import probatio

from .const import REGION_CUSTOM, REGION_URLS


def validate_timeout(value: Any) -> int:
    """Accept bounded integer seconds, never bools/floats or coerced strings."""
    if type(value) is not int or not 1 <= value <= 300:
        raise probatio.Invalid("invalid_timeout")
    return value


def _validated_api_host(value: str) -> str:
    """Validate syntax before translating boundary errors."""
    value = (value or "").strip()
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if (
        parsed.scheme not in ("", "http", "https")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError
    if (
        parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.port is not None
    ):
        raise ValueError
    host = parsed.hostname
    if not host or any(char.isspace() for char in value):
        raise ValueError
    try:
        ipaddress.ip_address(host)
    except ValueError:
        host = host.encode("idna").decode("ascii").lower()
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError from None
    # The SDK interpolates this directly after https://; retain IPv6 brackets.
    return f"[{host}]" if ":" in host else host


def _normalize_api_host(value: str) -> str:
    """Normalize an API host string (no scheme, no trailing slash/space)."""
    try:
        result = _validated_api_host(value)
    except (ValueError, UnicodeError) as err:
        raise probatio.Invalid("invalid_url") from err
    else:
        return result


def resolve_api_host(region: str, custom_url: str | None) -> str:
    """Resolve concrete API host from region/custom selection."""
    if region == REGION_CUSTOM:
        return _normalize_api_host(custom_url or "")
    return REGION_URLS[region]
