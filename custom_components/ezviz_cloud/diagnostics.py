"""Offline, credential-free diagnostics for EZVIZ."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TIMEOUT
from homeassistant.core import HomeAssistant

from .const import DEFAULT_TIMEOUT, DOMAIN, SETUP_DIAGNOSTICS
from .runtime import EzvizConfigEntry

REDACTED = "**REDACTED**"

# async_redact_data walks nested values, but it cannot redact identifiers used
# as mapping keys. Top-level device keys are therefore replaced with indexes
# before this set is applied.
TO_REDACT = {
    "accessToken",
    "api_url",
    "CLOUD",
    "device_id",
    "deviceName",
    "device_name",
    "deviceSerial",
    "encryptPwd",
    "encrypted_pwd_hash",
    "enc_key",
    "feature_code",
    "fullSerial",
    "CHANNEL",
    "KMS",
    "ip_address",
    "last_alarm_pic",
    "last_alarm_time",
    "localIp",
    "local_ip",
    "mac",
    "mac_address",
    "name",
    "netIp",
    "nickname",
    "P2P",
    "password",
    "picUrl",
    "resourceId",
    "rf_session_id",
    "serial",
    "session_id",
    "ssid",
    "superDeviceSerial",
    "TIME_PLAN",
    "token",
    "user_id",
    "userName",
    "username",
    "verification_code",
    "VTM",
    "VIDEO_QUALITY",
    "QOS",
    "wanIp",
    "wan_ip",
}


def _package_version(package: str) -> str:
    """Return an installed package version without making diagnostics fail."""
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def _redact_identifier(value: Any, identifier: str) -> Any:
    """Remove a device identifier even when it is used as a nested key."""
    if isinstance(value, dict):
        return {
            REDACTED if key == identifier else key: _redact_identifier(item, identifier)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_identifier(item, identifier) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_identifier(item, identifier) for item in value)
    return REDACTED if value == identifier else value


def _coordinator_diagnostics(coordinator: Any) -> dict[str, Any]:
    """Summarize polling health and redact cached devices without cloud I/O."""
    data = coordinator.data
    if isinstance(data, dict):
        devices = [
            {
                "device_index": index,
                "data": async_redact_data(
                    _redact_identifier(device, identifier), TO_REDACT
                ),
            }
            for index, (identifier, device) in enumerate(data.items(), start=1)
        ]
    else:
        devices = [{"device_index": 1, "data": REDACTED}]

    last_exception = getattr(coordinator, "last_exception", None)
    update_interval = getattr(coordinator, "update_interval", None)
    return {
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_exception_type": (
            type(last_exception).__name__ if last_exception is not None else None
        ),
        "last_update_attempt_at": getattr(
            coordinator, "last_update_attempt_at", None
        ),
        "last_update_success_at": getattr(
            coordinator, "last_update_success_at", None
        ),
        "update_interval_seconds": (
            update_interval.total_seconds() if update_interval is not None else None
        ),
        "device_count": len(data) if isinstance(data, dict) else None,
        "devices": devices,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EzvizConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without contacting EZVIZ or exposing identifiers."""
    result: dict[str, Any] = {
        "integration": {
            "config_entry_version": entry.version,
            "config_entry_minor_version": entry.minor_version,
            "pyezvizapi_version": _package_version("pyezvizapi"),
            "configured_timeout_seconds": entry.options.get(
                CONF_TIMEOUT, DEFAULT_TIMEOUT
            ),
        }
    }

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        setup = (
            hass.data.get(DOMAIN, {})
            .get(SETUP_DIAGNOSTICS, {})
            .get(entry.entry_id)
        )
        result["setup"] = setup or {"state": "not_loaded"}
        return result

    result.update(
        {
            "setup": {"state": "loaded"},
            "polling": _coordinator_diagnostics(runtime.coordinator),
            "push": runtime.push.diagnostics(),
            "credential_storage": runtime.token_store.diagnostics(),
        }
    )
    return result
