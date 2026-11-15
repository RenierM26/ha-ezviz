"""RTSP service operations using the shared local-device connection profile."""

from collections.abc import Mapping
from contextlib import suppress
from time import monotonic, sleep
from typing import Any

from pyezvizapi.exceptions import PyEzvizError

from .local import LocalDeviceConfig

RTSP_RECOVERY_TIMEOUT = 75.0
RTSP_RECOVERY_INTERVAL = 5.0


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
    """Write at most once, then confirm observed state after services recover."""
    client = config.client(serial)
    before = client.services_switch_state()
    if _known_rtsp(before.raw) is enabled:
        return enabled
    # Firmware can apply the change and restart services before returning an
    # ACK. The response alone cannot prove either success or failure.
    with suppress(PyEzvizError, OSError, ValueError):
        client.services_switch_set(before.raw, rtsp=int(enabled))
    # Only reads are retried, off-loop under the shared device lock. A matching
    # observation is authoritative even when the write response was lost.
    # Live Husky disable ACKs successfully, but management can remain unavailable
    # for about a minute. Do not mistake that recovery interval for a refusal.
    deadline = monotonic() + RTSP_RECOVERY_TIMEOUT
    while monotonic() < deadline:
        with suppress(PyEzvizError, OSError, ValueError):
            if _known_rtsp(client.services_switch_state().raw) is enabled:
                return enabled
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(RTSP_RECOVERY_INTERVAL, remaining))
    raise PyEzvizError("RTSP change outcome could not be verified")
