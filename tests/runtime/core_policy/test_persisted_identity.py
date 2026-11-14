"""Durable installation identity through native HA flows, storage and SDK restore."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import atomicwrites
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import config_flow, tokens
from custom_components.ezviz_cloud.const import CONF_SDK_TOKEN, DOMAIN
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import EzvizAuthTokenExpired, EzvizAuthVerificationCode
import pytest

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady

from tests.runtime import test_config_flow as controls
from tests.runtime.test_token_identity import identity_token

integration = controls.integration
sdk = controls.sdk


@pytest.mark.parametrize("source", ["entry", "private_store"])
async def test_adopt_existing_identity_without_rewriting_credentials(
    hass: HomeAssistant, source: str
) -> None:
    """Trusted channel-99 credentials survive a MAC mismatch without relabeling."""
    full = identity_token(feature_code="1" * 32)
    entry = controls.add_entry(
        hass, data={"type": controls.ATTR_TYPE_CLOUD, CONF_SDK_TOKEN: full}
    )
    owner = tokens.TokenPersistence(hass, entry)
    if source == "private_store":
        await owner.async_save(full)
    before = deepcopy(dict(entry.data))
    saved = await owner.async_load()
    selected = await owner.async_identity(saved)
    assert selected == full["feature_code"]
    client = EzvizClient(token=saved, feature_code=selected)
    assert client.export_token() == full
    client.close_session()
    assert await owner.async_load() == full
    assert dict(entry.data) == before
    account = hashlib.sha256(entry.unique_id.encode()).hexdigest()
    path = Path(tokens._identity_state(hass, account).store.path)
    assert json.loads(path.read_text())["data"] == {"feature_code": selected}
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mfa", [False, True])
async def test_first_login_identity_is_on_disk_before_sdk_and_reused_for_mfa(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mfa: bool
) -> None:
    """Use real SDK constructors and verify a durable identity precedes auth IO."""
    selected: list[str] = []
    snapshots: list[dict[str, Any]] = []

    def build(
        *, account: str, password: str, url: str, timeout: int, feature_code: str
    ) -> EzvizClient:
        code = feature_code
        account_hash = hashlib.sha256(account.encode()).hexdigest()
        path = Path(tokens._identity_state(hass, account_hash).store.path)
        assert json.loads(path.read_text())["data"]["feature_code"] == code
        selected.append(code)
        client = EzvizClient(
            account=account,
            password=password,
            url=url,
            timeout=timeout,
            feature_code=feature_code,
        )

        def login(_code: str | None = None) -> dict[str, Any]:
            if mfa and len(selected) == 1:
                raise EzvizAuthVerificationCode
            token = identity_token(feature_code=code)
            snapshots.append(token)
            return token

        monkeypatch.setattr(client, "enable_channel99", login)
        return client

    monkeypatch.setattr(config_flow, "EzvizClient", build)
    result = await controls.start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], controls.USER
    )
    if mfa:
        assert result["step_id"] == "user_mfa_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    owner = tokens.TokenPersistence(hass, entry)
    assert await owner.async_load() == snapshots[-1]
    assert await owner.async_identity() == selected[0]
    assert len(set(selected)) == 1
    assert selected[0] not in json.dumps(entry.as_dict())


@pytest.mark.parametrize(
    "step", ["user", "user_mfa_confirm", "reauth_confirm", "reauth_mfa"]
)
async def test_identity_storage_failure_stops_each_login_path_before_sdk(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, sdk: MagicMock, step: str
) -> None:
    """Identity fsync failure renders the existing actionable storage-error form."""
    manager = hass.config_entries.flow
    if step.startswith("reauth"):
        entry = controls.add_entry(hass)
        result = await manager.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        payload = {"username": entry.unique_id, "password": "new"}
    else:
        result = await controls.start_user(hass)
        payload = controls.USER
    if step in ("user_mfa_confirm", "reauth_mfa"):
        sdk.return_value.enable_channel99.side_effect = EzvizAuthVerificationCode()
        result = await manager.async_configure(result["flow_id"], payload)
        assert result["step_id"] == step
        payload = {"sms_code": "123456"}
    count = sdk.call_count
    failed = AsyncMock(side_effect=OSError("synthetic identity fsync failure"))
    if step.startswith("reauth"):
        monkeypatch.setattr(tokens.TokenPersistence, "async_identity", failed)
    else:
        monkeypatch.setattr(config_flow, "async_login_identity", failed)
    result = await manager.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    assert result["errors"] == {"base": "cannot_save_credentials"}
    assert sdk.call_count == count


@pytest.mark.parametrize(
    "record", ["invalid", {"feature_code": "wrong"}, {"feature_code": "A" * 32}]
)
async def test_corrupt_identity_is_not_silently_rotated(
    hass: HomeAssistant, record: object
) -> None:
    """An existing but corrupt anchor needs recovery, not a new host identity."""
    account = hashlib.sha256(b"account").hexdigest()
    state = tokens._identity_state(hass, account)
    await state.store.async_save(record)
    before = Path(state.store.path).read_bytes()
    with pytest.raises(OSError, match="Invalid saved EZVIZ installation identity"):
        await tokens.async_login_identity(hass, "account")
    assert Path(state.store.path).read_bytes() == before


async def test_concurrent_and_cancelled_first_login_preserve_one_durable_identity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel the waiter, not the owned atomic save or its shared account lock."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = tokens.AcknowledgedStore.async_save

    async def blocked(store: tokens.AcknowledgedStore, data: dict[str, str]) -> None:
        entered.set()
        await release.wait()
        await original(store, data)

    monkeypatch.setattr(tokens.AcknowledgedStore, "async_save", blocked)
    first = asyncio.create_task(tokens.async_login_identity(hass, "account"))
    await asyncio.wait_for(entered.wait(), 2)
    others = [
        asyncio.create_task(tokens.async_login_identity(hass, "account"))
        for _ in range(3)
    ]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    codes = await asyncio.wait_for(asyncio.gather(*others), 5)
    assert len(set(codes)) == 1
    assert await tokens.async_login_identity(hass, "account") == codes[0]


async def test_saved_identity_wins_over_conflicting_token_and_account_removal_cleans_it(
    hass: HomeAssistant,
) -> None:
    """Keep the anchor, reject a conflicting token, then clean up via Store API."""
    full = identity_token(feature_code="1" * 32)
    entry = controls.add_entry(
        hass, data={"type": controls.ATTR_TYPE_CLOUD, CONF_SDK_TOKEN: full}
    )
    owner = tokens.TokenPersistence(hass, entry)
    assert await owner.async_identity() == "1" * 32
    await owner.async_save(identity_token(feature_code="2" * 32))
    assert await owner.async_identity() == "1" * 32
    with pytest.raises(EzvizAuthTokenExpired):
        EzvizClient(
            token=await owner.async_load(), feature_code=await owner.async_identity()
        )
    account = hashlib.sha256(entry.unique_id.encode()).hexdigest()
    path = Path(tokens._identity_state(hass, account).store.path)
    await owner.async_remove()
    assert not path.exists()
    assert await tokens.async_login_identity(hass, entry.unique_id) != "1" * 32


async def test_real_ha_store_and_sdk_restore_across_processes_with_changed_mac(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Fresh Python/HA processes keep identity and complete token across NIC changes."""
    script = r"""
import asyncio, json, socket, sys, uuid
request = json.loads(sys.stdin.read())
uuid.getnode = lambda: request["node"]
def forbidden(*args, **kwargs):
    raise AssertionError("Identity restart tests must not contact devices or cloud")
socket.socket.connect = forbidden
socket.getaddrinfo = forbidden
from homeassistant.core import CoreState, HomeAssistant
from custom_components.ezviz_cloud.tokens import async_login_identity
from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import FEATURE_CODE, PROFILE
async def main():
    hass = HomeAssistant(request["directory"])
    try:
        selected = await async_login_identity(hass, "fixture@example.invalid")
        token = request.get("token") or {"session_id":"synthetic", "rf_session_id":"refresh", "username":"internal", "api_url":"api.example.invalid", "push_profile":PROFILE, "feature_code":selected, "push_state":{"counter":9}}
        client = EzvizClient(token=token, feature_code=selected)
        exported = client.export_token()
        client.close_session()
        print(json.dumps({"identity":selected, "host":FEATURE_CODE, "token":exported, "unchanged":exported==token}))
    finally:
        await hass.async_stop()
asyncio.run(main())
"""

    async def invoke(node: int, token: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await hass.async_add_executor_job(
            lambda: subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(
                    {
                        "node": node,
                        "token": token,
                        "directory": str(tmp_path / "restart"),
                    }
                ),
                text=True,
                capture_output=True,
                timeout=20,
                check=True,
            )
        )
        return json.loads(result.stdout)

    first = await invoke(0x001122334455)
    resumed = await invoke(0x001122334466, first["token"])
    assert resumed["host"] != first["host"]
    assert resumed["identity"] == first["identity"]
    assert resumed["unchanged"]
    assert resumed["token"] == first["token"]


async def test_real_identity_fsync_failure_prevents_first_login_then_allows_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, sdk: MagicMock
) -> None:
    """A real Store write failure cannot be acknowledged before SDK authentication."""
    result = await controls.start_user(hass)
    original = atomicwrites._proper_fsync

    def failed(_fd: int) -> None:
        raise OSError("synthetic identity fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(atomicwrites, "_proper_fsync", failed)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], controls.USER
        )
    assert result["errors"] == {"base": "cannot_save_credentials"}
    sdk.assert_not_called()
    assert atomicwrites._proper_fsync is original
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], controls.USER
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sdk.assert_called_once()


async def test_setup_identity_storage_failure_is_retryable_without_token_mutation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a corrupt identity before constructing a runtime/client or reauth flow."""
    full = identity_token(feature_code="1" * 32)
    entry = controls.add_entry(
        hass, version=5, data={"type": controls.ATTR_TYPE_CLOUD, CONF_SDK_TOKEN: full}
    )
    owner = tokens.TokenPersistence(hass, entry)
    state = tokens._identity_state(
        hass, hashlib.sha256(entry.unique_id.encode()).hexdigest()
    )
    await state.store.async_save({"feature_code": "corrupt"})
    client = MagicMock()
    monkeypatch.setattr(ezviz_cloud, "EzvizClient", client)
    with pytest.raises(ConfigEntryNotReady):
        await ezviz_cloud.async_setup_entry(hass, entry)
    client.assert_not_called()
    assert await owner.async_load() == full
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize("mfa", [False, True])
async def test_reauthentication_and_mfa_keep_the_established_identity(
    hass: HomeAssistant, sdk: MagicMock, mfa: bool
) -> None:
    """Fresh credentials retain the selected installation identity on every attempt."""
    old = identity_token(feature_code="1" * 32, push_state={"counter": 99})
    entry = controls.add_entry(
        hass, version=5, data={"type": controls.ATTR_TYPE_CLOUD, CONF_SDK_TOKEN: old}
    )
    fresh = identity_token(
        feature_code="1" * 32, session_id="fresh", push_state={"counter": 0}
    )
    sdk.return_value.enable_channel99.return_value = fresh
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    if mfa:
        sdk.return_value.enable_channel99.side_effect = EzvizAuthVerificationCode()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": entry.unique_id, "password": "not-persisted"}
    )
    if mfa:
        assert result["step_id"] == "reauth_mfa"
        sdk.return_value.enable_channel99.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
    assert result["reason"] == "reauth_successful"
    assert all(call.kwargs["feature_code"] == "1" * 32 for call in sdk.call_args_list)
    owner = tokens.TokenPersistence(hass, entry)
    assert await owner.async_identity() == "1" * 32
    assert await owner.async_load() == fresh
    client = EzvizClient(
        token=await owner.async_load(), feature_code=await owner.async_identity()
    )
    assert client.export_token() == fresh
    client.close_session()
    assert "not-persisted" not in json.dumps(entry.as_dict())


async def test_shutdown_deferred_identity_is_never_treated_as_durable_on_retry(
    hass: HomeAssistant,
) -> None:
    """Store may return pending in-memory data; retry still needs a real write ACK."""
    account = hashlib.sha256(b"account").hexdigest()
    state = tokens._identity_state(hass, account)
    hass.set_state(CoreState.stopping)
    try:
        for _attempt in range(2):
            with pytest.raises(OSError, match="durable write"):
                await tokens.async_login_identity(hass, "account")
        assert not Path(state.store.path).exists()
    finally:
        hass.set_state(CoreState.running)
    selected = await tokens.async_login_identity(hass, "account")
    assert json.loads(Path(state.store.path).read_text())["data"] == {
        "feature_code": selected
    }
