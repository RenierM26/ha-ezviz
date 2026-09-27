"""Diagnostics remain useful during outages and never expose credentials."""

from datetime import timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.ezviz_cloud.const import DOMAIN, SETUP_DIAGNOSTICS
from custom_components.ezviz_cloud.diagnostics import (
    REDACTED,
    async_get_config_entry_diagnostics,
)
from homeassistant.core import HomeAssistant


@pytest.mark.asyncio
async def test_loaded_diagnostics_are_offline_and_redact_keys_and_values(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    cloud_call = Mock(side_effect=AssertionError("diagnostics must not contact EZVIZ"))
    coordinator = SimpleNamespace(
        data={
            "CAMERA-SERIAL-AS-KEY": {
                "serial": "CAMERA-SERIAL-AS-VALUE",
                "deviceSerial": "API-SERIAL",
                "name": "Private camera name",
                "status": "online",
                "nested": {"session_id": "session-secret"},
                "lookup": {"CAMERA-SERIAL-AS-KEY": "online"},
            }
        },
        last_update_success=False,
        last_exception=TimeoutError("private endpoint"),
        last_update_attempt_at="2026-09-27T01:02:03+00:00",
        last_update_success_at="2026-09-27T00:55:00+00:00",
        update_interval=timedelta(seconds=30),
        ezviz_client=SimpleNamespace(get_device_infos=cloud_call),
    )
    runtime = SimpleNamespace(
        coordinator=coordinator,
        push=SimpleNamespace(
            diagnostics=Mock(
                return_value={
                    "state": "monitoring",
                    "events_received": 3,
                    "sdk": {"state": "retry_wait", "last_error_type": "TimeoutError"},
                }
            )
        ),
        token_store=SimpleNamespace(
            diagnostics=Mock(
                return_value={
                    "state": "ready",
                    "source": "private_store",
                    "file_present": True,
                }
            )
        ),
    )
    entry: Any = SimpleNamespace(
        entry_id="entry",
        version=4,
        minor_version=1,
        options={"timeout": 12},
        runtime_data=runtime,
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    cloud_call.assert_not_called()
    assert result["setup"] == {"state": "loaded"}
    assert result["polling"]["last_update_success"] is False
    assert result["polling"]["last_exception_type"] == "TimeoutError"
    assert result["polling"]["last_update_attempt_at"] == "2026-09-27T01:02:03+00:00"
    assert result["polling"]["last_update_success_at"] == "2026-09-27T00:55:00+00:00"
    assert result["polling"]["device_count"] == 1
    assert result["polling"]["devices"] == [
        {
            "device_index": 1,
            "data": {
                "serial": REDACTED,
                "deviceSerial": REDACTED,
                "name": REDACTED,
                "status": "online",
                "nested": {"session_id": REDACTED},
                "lookup": {REDACTED: "online"},
            },
        }
    ]
    encoded = json.dumps(result)
    for secret in (
        "CAMERA-SERIAL-AS-KEY",
        "CAMERA-SERIAL-AS-VALUE",
        "API-SERIAL",
        "Private camera name",
        "session-secret",
        "private endpoint",
    ):
        assert secret not in encoded


@pytest.mark.asyncio
async def test_setup_failure_diagnostics_work_without_runtime(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    failure = {
        "state": "failed",
        "stage": "credential_storage",
        "error_type": "OSError",
        "recorded_at": "2026-09-27T00:00:00+00:00",
    }
    hass.data[DOMAIN] = {SETUP_DIAGNOSTICS: {"entry": failure}}
    entry: Any = SimpleNamespace(
        entry_id="entry", version=4, minor_version=1, options={}
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["setup"] == failure
    assert "polling" not in result
    assert "push" not in result
    assert "credential_storage" not in result
