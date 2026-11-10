"""Real SDK host-token validation through native HA setup, reauth and restart."""

from copy import deepcopy
from datetime import UTC, datetime
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi import DeviceSnapshot
from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import FEATURE_CODE, PROFILE
import pytest
import test_config_flow as controls

from custom_components import ezviz_cloud
from custom_components.ezviz_cloud.const import CONF_SDK_TOKEN, DOMAIN
from custom_components.ezviz_cloud.tokens import TokenPersistence
from homeassistant.config_entries import ConfigEntries, ConfigEntryState, current_entry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

integration = controls.integration


def identity_token(**changes):
    return {
        "session_id": "synthetic-session",
        "rf_session_id": "synthetic-refresh",
        "username": "internal",
        "user_id": "internal",
        "api_url": "api.example.invalid",
        "push_profile": PROFILE,
        "feature_code": FEATURE_CODE,
        "push_state": {"counter": 7},
        **changes,
    }


@pytest.mark.parametrize("invalid", ["changed", "missing"])
@pytest.mark.parametrize("source", ["entry", "private_store"])
async def test_constructor_auth_failure_starts_one_reauth_without_modifying_tokens(
    hass, invalid, source, caplog
):
    full = identity_token()
    bad = {**full, "feature_code": "different-host"}
    if invalid == "missing":
        bad.pop("feature_code")
    entry = controls.add_entry(
        hass,
        data={
            "type": controls.ATTR_TYPE_CLOUD,
            "session_id": full["session_id"],
            "rf_session_id": full["rf_session_id"],
            "url": full["api_url"],
            "user_id": "internal",
            CONF_SDK_TOKEN: bad if source == "entry" else full,
        },
    )
    if source == "private_store":
        await TokenPersistence(hass, entry).async_save(bad)
    before = deepcopy(entry.as_dict())
    async with entry.setup_lock:
        await entry.async_setup(hass)
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(progress) == 1
    assert progress[0]["context"]["source"] == "reauth"
    assert progress[0]["context"]["entry_id"] == entry.entry_id
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "saved_login_invalid"
    assert entry.as_dict() == before
    assert await TokenPersistence(hass, entry).async_load() == bad
    assert not hasattr(entry, "runtime_data")
    assert not hass.data[DOMAIN]["_runtimes"]
    assert "Error setting up entry" not in caplog.text
    assert "different-host" not in caplog.text


async def test_native_reauth_replaces_old_identity_and_stale_private_store(hass, monkeypatch):
    old = identity_token(feature_code="old-host", push_state={"counter": 99})
    fresh = identity_token(
        session_id="fresh-session", rf_session_id="fresh-refresh", push_state={"counter": 0}
    )
    entry = controls.add_entry(
        hass,
        data={
            "type": controls.ATTR_TYPE_CLOUD,
            "session_id": old["session_id"],
            "rf_session_id": old["rf_session_id"],
            "url": old["api_url"],
            "user_id": "internal",
            CONF_SDK_TOKEN: old,
        },
    )
    await TokenPersistence(hass, entry).async_save(old)
    async with entry.setup_lock:
        await entry.async_setup(hass)
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(progress) == 1
    api = SimpleNamespace(enable_channel99=MagicMock(return_value=fresh), close_session=MagicMock())
    monkeypatch.setattr(controls.flow, "EzvizClient", MagicMock(return_value=api))
    result = await hass.config_entries.flow.async_configure(
        progress[0]["flow_id"], {"username": entry.unique_id, "password": "not-stored"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SDK_TOKEN] == fresh
    assert await TokenPersistence(hass, entry).async_load() == fresh
    # The actual published SDK constructor accepts the replacement; no validation bypass.
    resumed = EzvizClient(token=deepcopy(await TokenPersistence(hass, entry).async_load()))
    resumed.close_session()
    assert "not-stored" not in json.dumps(entry.as_dict())
    api.close_session.assert_called_once_with()


async def test_matching_host_full_token_survives_real_setup_and_storage_restart(hass, monkeypatch):
    full = identity_token()
    rotated = identity_token(session_id="rotated-session", push_state={"counter": 9})
    entry = controls.add_entry(
        hass,
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        data={
            "type": controls.ATTR_TYPE_CLOUD,
            "session_id": full["session_id"],
            "rf_session_id": full["rf_session_id"],
            "url": full["api_url"],
            "user_id": "internal",
            CONF_SDK_TOKEN: full,
        },
    )
    hass.config_entries.async_update_entry(entry, title="Persisted identity")
    await TokenPersistence(hass, entry).async_save(rotated)
    observed = []
    clients = []

    def build(**kwargs):
        observed.append(deepcopy(kwargs["token"]))
        client = EzvizClient(**kwargs)
        monkeypatch.setattr(client, "login", client.export_token)
        monkeypatch.setattr(
            client, "poll_devices", lambda _options: DeviceSnapshot(1, datetime.now(UTC), {}, {})
        )
        clients.append(client)
        return client

    monkeypatch.setattr(ezviz_cloud, "EzvizClient", build)
    monkeypatch.setattr(ezviz_cloud, "ImageProxyView", lambda _hass: object())
    monkeypatch.setattr(ezviz_cloud.EzvizMqttHandler, "async_start", lambda _self: None)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    hass.http = SimpleNamespace(register_view=lambda _view: None)
    context = current_entry.set(entry)
    try:
        assert await ezviz_cloud.async_setup_entry(hass, entry)
    finally:
        current_entry.reset(context)
    await hass.async_stop()
    restored = HomeAssistant(hass.config.config_dir)
    restored.config_entries = ConfigEntries(restored, {})
    try:
        await restored.config_entries.async_initialize()
        saved = restored.config_entries.async_get_entry(entry.entry_id)
        assert saved is not None
        assert await TokenPersistence(restored, saved).async_load() == rotated
        assert saved.data[CONF_SDK_TOKEN] == full
        resumed = EzvizClient(token=await TokenPersistence(restored, saved).async_load())
        assert resumed.export_token() == rotated
        resumed.close_session()
    finally:
        await restored.async_stop()
    assert observed == [rotated]
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_separate_python_processes_preserve_same_host_and_reject_changed_host(hass):
    """Real SDK imports in fresh processes; synthetic NICs, no network, no global patches."""
    script = r"""
import json, socket, sys, uuid
request = json.loads(sys.stdin.read())
uuid.getnode = lambda: request["node"]
def no_network(*args, **kwargs):
    raise AssertionError("Subprocess SDK identity test must not access the network")
socket.socket.connect = no_network
socket.getaddrinfo = no_network
from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import FEATURE_CODE, PROFILE
from pyezvizapi.exceptions import EzvizAuthTokenExpired
token = request.get("token") or {"session_id": "synthetic", "rf_session_id": "synthetic-refresh", "username": "fixture", "user_id": "fixture", "api_url": "api.example.invalid", "push_profile": PROFILE, "feature_code": FEATURE_CODE, "push_state": {"counter": 9}}
try:
    client = EzvizClient(token=token)
except EzvizAuthTokenExpired:
    print(json.dumps({"accepted": False}))
else:
    exported = client.export_token()
    client.close_session()
    print(json.dumps({"accepted": True, "token": exported, "unchanged": exported == token}))
"""

    async def invoke(node, token=None):
        result = await hass.async_add_executor_job(
            lambda: subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps({"node": node, "token": token}),
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
            )
        )
        return json.loads(result.stdout)

    initial = await invoke(0x001122334455)
    same_host = await invoke(0x001122334455, initial["token"])
    assert same_host["accepted"] and same_host["unchanged"]
    changed_host = await invoke(0x001122334466, initial["token"])
    assert changed_host == {"accepted": False}
    fresh = await invoke(0x001122334466)
    assert fresh["token"]["feature_code"] != initial["token"]["feature_code"]
    assert (await invoke(0x001122334466, fresh["token"]))["accepted"]


@pytest.mark.parametrize(
    "error", [controls.InvalidURL, controls.HTTPError, controls.PyEzvizError, OSError, TimeoutError]
)
async def test_constructor_connectivity_errors_retry_without_reauth_or_token_loss(
    hass, monkeypatch, error
):
    full = identity_token()
    entry = controls.add_entry(
        hass,
        data={
            "type": controls.ATTR_TYPE_CLOUD,
            "session_id": full["session_id"],
            "rf_session_id": full["rf_session_id"],
            "url": full["api_url"],
            "user_id": "internal",
            CONF_SDK_TOKEN: full,
        },
    )
    before = deepcopy(entry.as_dict())
    monkeypatch.setattr(
        ezviz_cloud, "EzvizClient", MagicMock(side_effect=error("synthetic failure"))
    )
    with pytest.raises(ConfigEntryNotReady):
        await ezviz_cloud.async_setup_entry(hass, entry)
    assert entry.as_dict() == before
    assert not hasattr(entry, "runtime_data")
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_constructor_mfa_expiry_requests_reauth(hass, monkeypatch):
    entry = controls.add_entry(hass)
    monkeypatch.setattr(
        ezviz_cloud, "EzvizClient", MagicMock(side_effect=controls.EzvizAuthVerificationCode())
    )
    with pytest.raises(ConfigEntryAuthFailed) as caught:
        await ezviz_cloud.async_setup_entry(hass, entry)
    assert caught.value.translation_key == "saved_login_invalid"
    assert not hasattr(entry, "runtime_data")
