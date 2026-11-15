"""Shared local-device authentication and serialized command-port execution."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any

from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.local.hcnetsdk.api import (
    HcNetSdkLanEndpoint,
    HcNetSdkPurePythonClient,
)

from .const import CONF_ENC_KEY, DEFAULT_CAMERA_USERNAME, DEFAULT_FETCH_MY_KEY
from .credentials import camera_options

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

CONF_LOCAL_CONTROL_AUTH = "local_control_auth"
LOCAL_TIMEOUT = 5.0


class LocalControlAuth(StrEnum):
    """Explicit credential source for current and future local controls."""

    DISABLED = "disabled"
    VERIFICATION_CODE = "verification_code"
    ENCRYPTION_KEY = "encryption_key"


def local_controls_eligible(data: Mapping[str, Any]) -> bool:
    """Conservatively exclude battery, doorbell and unknown camera families."""
    return data.get("device_category") == DeviceCatagories.CAMERA_DEVICE_CATEGORY.value


@dataclass(frozen=True)
class LocalDeviceConfig:
    """One operation's resolved LAN profile; credentials are never duplicated in options."""

    host: str
    username: str
    password: str = field(repr=False)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> LocalDeviceConfig:
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
            or password == DEFAULT_FETCH_MY_KEY
            or "\0" in password
            or len(password.encode()) > 1024
        ):
            raise ValueError("Invalid local camera configuration")
        return cls(host, username, password)

    @classmethod
    def from_camera_options(
        cls, options: Mapping[str, Any], data: Mapping[str, Any]
    ) -> LocalDeviceConfig:
        """Select only the requested camera credential, never fall back to another key."""
        auth = LocalControlAuth(
            options.get(CONF_LOCAL_CONTROL_AUTH, LocalControlAuth.DISABLED)
        )
        if auth is LocalControlAuth.DISABLED or not local_controls_eligible(data):
            raise PyEzvizError("Local device controls are disabled or unsupported")
        password = options.get(
            "password" if auth is LocalControlAuth.VERIFICATION_CODE else CONF_ENC_KEY
        )
        return cls.from_options(
            {
                "host": data.get("local_ip"),
                "username": options.get("username", DEFAULT_CAMERA_USERNAME),
                "password": password,
            }
        )

    def client(self, serial: str) -> HcNetSdkPurePythonClient:
        """Use TCP 8000 without opening services or guessing authentication."""
        return HcNetSdkPurePythonClient(
            HcNetSdkLanEndpoint(serial, self.host, command_port=8000),
            self.password,
            username=self.username,
            timeout=LOCAL_TIMEOUT,
        )


class LocalDevice:
    """Shared per-camera executor for RTSP and future local device functions."""

    def __init__(self, account: EzvizDataUpdateCoordinator, serial: str) -> None:
        """Keep a lock and owner, not a long-lived credential or network session."""
        self.account = account
        self.serial = serial
        self._lock = asyncio.Lock()

    def configuration(self) -> LocalDeviceConfig:
        """Read the current source, key and SDK-observed address for each operation."""
        entry = self.account.config_entry
        if entry is None:
            raise PyEzvizError("Local device controls require a config entry")
        return LocalDeviceConfig.from_camera_options(
            camera_options(entry, self.serial), self.account.device_data(self.serial)
        )

    async def async_execute(
        self,
        func: Callable,
        *args: Any,
        options: Mapping[str, Any] | None = None,
        precondition: Callable[[], Any] | None = None,
    ) -> Any:
        """Retain the per-camera lock until the owned SDK worker really completes."""

        async def operation() -> Any:
            async with self._lock:
                if precondition is not None:
                    precondition()
                config = (
                    self.configuration()
                    if options is None
                    else LocalDeviceConfig.from_camera_options(
                        options, self.account.device_data(self.serial)
                    )
                )
                return await self.account.async_execute(
                    func, config, self.serial, *args
                )

        return await self.account.async_execute_owned(operation)
