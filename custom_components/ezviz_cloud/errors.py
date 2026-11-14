"""Privacy-safe EZVIZ failures shared by polling and action adapters."""

import re

from pyezvizapi.exceptions import InvalidHost, InvalidURL
import requests

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_API_CODE = re.compile(r"\bresponse code=(-?[0-9]{1,12}), fields=[0-9]+\b", re.ASCII)


def error_category(error: Exception) -> tuple[str, str]:
    """Extract only finite categories/codes, never URLs, bodies or vendor text.

    SDK HTTPError has an empty message and chains requests.HTTPError. The
    published SDK exposes API codes only in its sanitized response summary.
    Walk explicit causes only, bounded even for malformed/cyclic chains.
    """
    current: BaseException | None = error
    for _ in range(8):
        if isinstance(current, requests.HTTPError) and current.response is not None:
            status = current.response.status_code
            if type(status) is int and 100 <= status <= 599:
                detail = f"HTTP {status}"
                if status == 403:
                    return "request_forbidden", detail
                if status in (408, 504):
                    return "request_timeout", detail
                return "request_http", detail
        if isinstance(current, (TimeoutError, requests.Timeout)):
            return "request_timeout", "timeout"
        if current is None:
            break
        current = current.__cause__
    if match := _API_CODE.search(str(error)):
        detail = f"API {int(match[1])}"
        return ("request_forbidden" if int(match[1]) == 403 else "request_api"), detail
    if isinstance(error, (InvalidHost, InvalidURL, OSError, requests.ConnectionError)):
        return "request_connection", type(error).__name__
    return "request_failed", type(error).__name__


def request_error(operation: str, error: Exception) -> HomeAssistantError:
    """Build a translated action error without logging it a second time."""
    key, detail = error_category(error)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"operation": operation, "detail": detail},
    )
