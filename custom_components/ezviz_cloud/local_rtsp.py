"""Opt-in RTSP configuration over the camera's local SDK command port."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import IPv4Address
from typing import Any

from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.local.hcnetsdk.api import (
    HcNetSdkLanEndpoint,
    HcNetSdkPurePythonClient,
)

CONF_LOCAL_RTSP = "local_rtsp"
LOCAL_TIMEOUT = 5.0
POLL_INTERVAL = timedelta(minutes=5)


class LocalRtspUnsupported(PyEzvizError):
    """The device did not report a recognized RTSP service setting."""


def local_rtsp_eligible(data: Mapping[str, Any]) -> bool:
    """Exclude battery families even when running from external power."""
    return data.get("device_category") == DeviceCatagories.CAMERA_DEVICE_CATEGORY.value


@dataclass(frozen=True)
class LocalRtspConfig:
    """Explicit LAN credentials, independent of RTSP/media/cloud credentials."""

    host: str
    username: str
    password: str = field(repr=False)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> LocalRtspConfig:
        """Validate literal unicast IPv4 and nonempty, bounded credentials."""
        if not isinstance(options["host"], str):
            raise TypeError("Camera host must be a literal IPv4 address")
        host = str(IPv4Address(options["host"]))
        address = IPv4Address(host)
        username, password = options["username"], options["password"]
        if (
            address.is_loopback
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
            or not isinstance(username, str)
            or not username.strip()
            or not isinstance(password, str)
            or not password
            or "\0" in password
            or len(password.encode()) > 1024
        ):
            raise ValueError("Invalid local camera configuration")
        return cls(host, username, password)

    def client(self, serial: str) -> HcNetSdkPurePythonClient:
        """Use only TCP 8000; never open a service or try alternate credentials."""
        return HcNetSdkPurePythonClient(
            HcNetSdkLanEndpoint(serial, self.host, command_port=8000),
            self.password,
            username=self.username,
            timeout=LOCAL_TIMEOUT,
        )


def _known_rtsp(raw: Mapping[str, Any] | None) -> bool:
    """SDK integer coercion must not turn absent/malformed state into off."""
    services = raw.get("servicesSwitch") if raw is not None else None
    value = services.get("rtsp") if isinstance(services, Mapping) else None
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    raise LocalRtspUnsupported("No recognized RTSP service setting")


def read_local_rtsp(config: LocalRtspConfig, serial: str) -> bool:
    """Read configuration only, not RTSP listener or playback health."""
    return _known_rtsp(config.client(serial).services_switch_state().raw)


def write_local_rtsp(config: LocalRtspConfig, serial: str, enabled: bool) -> bool:
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
