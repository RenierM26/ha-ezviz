"""RTSP service operations using the shared local-device connection profile."""

from collections.abc import Mapping
from typing import Any

from pyezvizapi.exceptions import PyEzvizError

from .local import LocalDeviceConfig


class LocalRtspUnsupported(PyEzvizError):
    """The device did not report a recognized RTSP service setting."""


def _known_rtsp(raw: Mapping[str, Any] | None) -> bool:
    """SDK integer coercion must not turn absent/malformed state into off."""
    services = raw.get("servicesSwitch") if raw is not None else None
    value = services.get("rtsp") if isinstance(services, Mapping) else None
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    raise LocalRtspUnsupported("No recognized RTSP service setting")


def read_local_rtsp(config: LocalDeviceConfig, serial: str) -> bool:
    """Read configuration only, not RTSP listener or playback health."""
    return _known_rtsp(config.client(serial).services_switch_state().raw)


def write_local_rtsp(config: LocalDeviceConfig, serial: str, enabled: bool) -> bool:
    """Read/modify/read back once, preserving every unrelated service field."""
    client = config.client(serial)
    before = client.services_switch_state()
    if _known_rtsp(before.raw) is enabled:
        return enabled
    response = client.services_switch_set(before.raw, rtsp=int(enabled))
    status = response.json().get("statusCode") if response.succeeded else None
    if type(status) is not int or status != 1:
        raise PyEzvizError("RTSP update was not acknowledged")
    observed = _known_rtsp(client.services_switch_state().raw)
    if observed is not enabled:
        raise PyEzvizError("RTSP update was not verified")
    return observed
