"""Config/options behavior through real HA flow managers; only SDK IO is fake."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)
import pytest

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud import config_flow as flow
from custom_components.ezviz_cloud.const import (
    ATTR_TYPE_CLOUD,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    REGION_URLS,
)
from custom_components.ezviz_cloud.tokens import TokenPersistence
from homeassistant import loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData

TOKEN = {
    "session_id": "new-session",
    "rf_session_id": "new-refresh",
    "username": "internal",
    "push_state": {"counter": 7},
}
USER = {"username": "fixture@example.invalid", "password": "never-persist", "region": "eu"}
CAMERA = {
    "username": "admin",
    "password": "sticker",
    "enc_key": "encryption",
    "rtsp_uses_verification_code": False,
    "ephemeral_test_rtsp": False,
    "ffmpeg_arguments": DEFAULT_FFMPEG_ARGUMENTS,
}


@pytest.fixture(autouse=True)
def integration(hass, monkeypatch):
    """Load our real handler; don't start platforms or resolve installed dependencies."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = flow
    monkeypatch.setattr(hass.config_entries, "async_setup", AsyncMock(return_value=True))
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.enable_channel99.return_value = deepcopy(TOKEN)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(flow, "EzvizClient", factory)
    return factory


def add_entry(hass, **overrides):
    fields = dict(
        data={
            "type": ATTR_TYPE_CLOUD,
            "session_id": "old",
            "rf_session_id": "old-refresh",
            "url": REGION_URLS["eu"],
            "user_id": "internal",
        },
        discovery_keys={},
        domain=DOMAIN,
        minor_version=1,
        options={"timeout": 41, "cameras": {"OTHER": {"password": "untouched"}}},
        source="user",
        state=ConfigEntryState.NOT_LOADED,
        subentries_data=(),
        title=USER["username"],
        unique_id=USER["username"],
        version=4,
    )
    fields.update(overrides)
    entry = ConfigEntry(**fields)
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


async def start_user(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


@pytest.mark.parametrize(
    ("region", "url", "host"),
    [
        ("eu", None, REGION_URLS["eu"]),
        ("ru", None, REGION_URLS["ru"]),
        ("custom", " https://api.example.invalid/ ", "api.example.invalid"),
        ("custom", "http://api.example.invalid/", "api.example.invalid"),
        ("custom", "api.example.invalid", "api.example.invalid"),
    ],
)
async def test_user_success(hass, sdk, region, url, host):
    result = await start_user(hass)
    payload = {**USER, "region": region, "timeout": 54}
    if url is not None:
        payload["url"] = url
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == USER["username"]
    assert entry.version == ezviz_cloud.TARGET_VERSION
    assert entry.data == {"type": ATTR_TYPE_CLOUD, "url": host}
    assert result["data"] == dict(entry.data)
    assert await TokenPersistence(hass, entry).async_load() == TOKEN
    assert entry.options == {"timeout": 54, "cameras": {}}
    sdk.assert_called_once_with(
        account=USER["username"], password=USER["password"], url=host, timeout=54
    )
    sdk.return_value.close_session.assert_called_once_with()
    sdk.return_value.login.assert_not_called()


@pytest.mark.parametrize("url", [None, "", " / ", "https://"])
async def test_invalid_host_then_retry(hass, sdk, url):
    result = await start_user(hass)
    payload = {**USER, "region": "custom"}
    if url is not None:
        payload["url"] = url
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["errors"] == {"base": "invalid_url"}
    sdk.assert_not_called()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["timeout"] == DEFAULT_TIMEOUT


async def test_duplicate_account(hass, sdk):
    entry = add_entry(hass)
    before = entry.as_dict()
    result = await start_user(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["reason"] == "already_configured"
    assert entry.as_dict() == before
    sdk.assert_not_called()


@pytest.mark.parametrize("reauth", [False, True])
@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidURL, "cannot_connect"),
        (HTTPError, "cannot_connect"),
        (PyEzvizError, "cannot_connect"),
        (RuntimeError, "unknown"),
        (EzvizAuthVerificationCode, "verification_required"),
    ],
)
async def test_login_errors_retry(hass, sdk, reauth, mfa, error, expected):
    if error is EzvizAuthVerificationCode and not mfa:
        # This case requests the MFA form; the remaining parameters exercise errors on it.
        expected = None
    entry = add_entry(hass) if reauth else None
    before = entry.as_dict() if entry else None
    manager = hass.config_entries.flow
    result = (
        await manager.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
        )
        if reauth
        else await start_user(hass)
    )
    submission = {"username": USER["username"], "password": USER["password"]} if reauth else USER
    if mfa:
        sdk.return_value.enable_channel99.side_effect = EzvizAuthVerificationCode()
        result = await manager.async_configure(result["flow_id"], submission)
        assert result["step_id"] == ("reauth_mfa" if reauth else "user_mfa_confirm")
        submission = {"sms_code": "123456"}
    sdk.return_value.enable_channel99.side_effect = error("synthetic failure")
    result = await manager.async_configure(result["flow_id"], submission)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == ({"base": expected} if expected else {})
    if entry:
        assert entry.as_dict() == before
    else:
        assert not hass.config_entries.async_entries(DOMAIN)
    sdk.return_value.enable_channel99.side_effect = None
    result = await manager.async_configure(
        result["flow_id"], {"sms_code": "654321"} if mfa or expected is None else submission
    )
    if reauth:
        assert result["reason"] == "reauth_successful"
        assert entry.data == {"type": ATTR_TYPE_CLOUD, "url": before["data"]["url"]}
        assert await TokenPersistence(hass, entry).async_load() == TOKEN
        assert entry.options == before["options"]
        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {"type": ATTR_TYPE_CLOUD, "url": REGION_URLS["eu"]}
        assert await TokenPersistence(hass, result["result"]).async_load() == TOKEN
        assert result["result"].unique_id == USER["username"]
        assert "password" not in result["data"]
    assert sdk.return_value.close_session.call_count == sdk.return_value.enable_channel99.call_count


async def start_camera(hass, monkeypatch, *, cameras=True, battery=False):
    entry = add_entry(hass)
    client = MagicMock()
    client.get_cam_key.return_value = "fetched-encryption"
    client.get_cam_auth_code.return_value = "fetched-sticker"

    async def execute(function, *args):
        return await hass.async_add_executor_job(function, *args)

    coordinator = SimpleNamespace(
        data={
            "CAM": {
                "name": "Porch",
                "local_ip": "192.0.2.10",
                "device_category": DeviceCatagories.BATTERY_CAMERA_DEVICE_CATEGORY.value
                if battery
                else "IPC",
            }
        }
        if cameras
        else {},
        ezviz_client=client,
        async_execute=execute,
        shutting_down=False,
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    rtsp = MagicMock()
    monkeypatch.setattr(flow, "TestRTSPAuth", rtsp)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "camera_select"})
    if cameras:
        assert result["step_id"] == "camera_select"
        result = await manager.async_configure(result["flow_id"], {"serial": "CAM"})
        assert result["step_id"] == "camera_edit"
    return entry, client, rtsp, result


@pytest.mark.parametrize("battery", [False, True])
@pytest.mark.parametrize("fetch", [False, True])
@pytest.mark.parametrize("verification", [False, True])
async def test_camera_success(hass, monkeypatch, battery, fetch, verification):
    entry, client, rtsp, result = await start_camera(hass, monkeypatch, battery=battery)
    defaults = result["data_schema"]({})
    assert defaults["ephemeral_test_rtsp"] is not battery
    payload = {
        **CAMERA,
        "rtsp_uses_verification_code": verification,
        "ephemeral_test_rtsp": not battery,
    }
    if fetch:
        payload.update(password=DEFAULT_FETCH_MY_KEY, enc_key=DEFAULT_FETCH_MY_KEY)
    result = await hass.config_entries.options.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = {k: v for k, v in payload.items() if k != "ephemeral_test_rtsp"}
    if fetch:
        expected.update(password="fetched-sticker", enc_key="fetched-encryption")
        client.get_cam_key.assert_called_once_with("CAM", smscode=None)
        client.get_cam_auth_code.assert_called_once_with("CAM", msg_auth_code=None, sender_type=3)
    else:
        client.get_cam_key.assert_not_called()
        client.get_cam_auth_code.assert_not_called()
    assert entry.options == {
        "timeout": 41,
        "cameras": {"OTHER": {"password": "untouched"}, "CAM": expected},
    }
    if battery:
        rtsp.assert_not_called()
        client.get_detection_sensibility.assert_not_called()
    else:
        rtsp.assert_called_once_with(
            "192.0.2.10", "admin", expected["password" if verification else "enc_key"]
        )
        rtsp.return_value.main.assert_called_once_with()
        client.get_detection_sensibility.assert_called_once_with("CAM")


async def test_no_cameras(hass, monkeypatch):
    _, _, _, result = await start_camera(hass, monkeypatch, cameras=False)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_cameras"


@pytest.mark.parametrize(
    "options",
    [{}, {"timeout": 41, "cameras": {"OTHER": {"enc_key": "keep"}}, "future_option": True}],
)
async def test_cloud_options(hass, options):
    entry = add_entry(hass, options=options)
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data={}))
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    assert result["data_schema"]({}) == {"timeout": options.get("timeout", DEFAULT_TIMEOUT)}
    result = await manager.async_configure(result["flow_id"], {"timeout": 67})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**options, "timeout": 67}


@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthTestResultFailed, "rtsp_auth_failed"),
        (DeviceException, "device_exception"),
        (InvalidURL, "cannot_connect"),
        (HTTPError, "cannot_connect"),
        (PyEzvizError, "cannot_connect"),
        (RuntimeError, "unknown"),
        (EzvizAuthVerificationCode, "verification_required"),
    ],
)
async def test_camera_errors_and_retry(hass, monkeypatch, mfa, error, expected):
    entry, client, rtsp, result = await start_camera(hass, monkeypatch)
    before = deepcopy(dict(entry.options))
    payload = {
        **CAMERA,
        "enc_key": DEFAULT_FETCH_MY_KEY,
        "password": DEFAULT_FETCH_MY_KEY,
        "ephemeral_test_rtsp": True,
    }
    manager = hass.config_entries.options
    if mfa:
        client.get_cam_key.side_effect = EzvizAuthVerificationCode()
        result = await manager.async_configure(result["flow_id"], payload)
        assert result["step_id"] == "camera_edit_2fa"
    client.get_cam_key.side_effect = None
    rtsp.return_value.main.side_effect = error("synthetic failure")
    submission = (
        {"cam_verification_2fa_code": "123456", "cam_encryption_2fa_code": "654321"}
        if mfa
        else payload
    )
    result = await manager.async_configure(result["flow_id"], submission)
    assert entry.options == before
    if error is RuntimeError:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "unknown"
        return
    assert result["type"] is FlowResultType.FORM
    if not mfa and error is not EzvizAuthVerificationCode:
        assert result["errors"] == {"base": expected}
    elif error is EzvizAuthVerificationCode:
        assert result["step_id"] == "camera_edit_2fa"
        assert result["errors"] == ({"base": expected} if mfa else {})
    else:
        assert result["step_id"] == "camera_edit"
        assert result["errors"] == {"base": expected}
        assert result["data_schema"]({})["enc_key"] == "fetched-encryption"
    rtsp.return_value.main.side_effect = None
    submission = (
        {"cam_verification_2fa_code": "123456", "cam_encryption_2fa_code": "654321"}
        if result["step_id"] == "camera_edit_2fa"
        else result["data_schema"]({})
    )
    result = await manager.async_configure(result["flow_id"], submission)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cameras"]["CAM"]["enc_key"] == "fetched-encryption"
    assert set(entry.options["cameras"]["CAM"]) == set(CAMERA) - {"ephemeral_test_rtsp"}


async def test_verification_code_mfa(hass, monkeypatch):
    """Device-auth SMS request then retry must pass one-time codes only to the SDK."""
    entry, client, _, result = await start_camera(hass, monkeypatch)
    client.get_cam_auth_code.side_effect = [EzvizAuthVerificationCode(), "resolved-sticker"]
    payload = {**CAMERA, "password": DEFAULT_FETCH_MY_KEY, "enc_key": DEFAULT_FETCH_MY_KEY}
    manager = hass.config_entries.options
    result = await manager.async_configure(result["flow_id"], payload)
    assert result["step_id"] == "camera_edit_2fa"
    client.get_2fa_check_code.assert_called_once_with(
        username=USER["username"], biz_type="DEVICE_AUTH_CODE"
    )
    result = await manager.async_configure(
        result["flow_id"],
        {"cam_verification_2fa_code": "123456", "cam_encryption_2fa_code": "654321"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    client.get_cam_auth_code.assert_called_with("CAM", msg_auth_code="123456", sender_type=0)
    assert entry.options["cameras"]["CAM"] == {
        key: value
        for key, value in {
            **CAMERA,
            "password": "resolved-sticker",
            "enc_key": "fetched-encryption",
        }.items()
        if key != "ephemeral_test_rtsp"
    }


async def test_camera_2fa_without_pending_returns_edit(hass, monkeypatch):
    """Explicitly exercise the defensive guard, unreachable through the normal menu."""
    _, _, _, result = await start_camera(hass, monkeypatch)
    handler = hass.config_entries.options._progress[result["flow_id"]]
    result = await handler.async_step_camera_edit_2fa()
    assert result["step_id"] == "camera_edit"
    assert result["errors"] == {}


async def test_reauth_missing_account(hass, sdk):
    entry = add_entry(hass, data={"type": "camera"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data={}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"
    sdk.assert_not_called()


@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize("other_account", [False, True])
async def test_reauth_targets_context_entry_and_repairs_legacy_id(hass, sdk, mfa, other_account):
    """Reauth cannot pick another account; old migrated entries lack cloud user_id."""
    target = add_entry(hass)
    hass.config_entries.async_update_entry(
        target, data={k: v for k, v in target.data.items() if k != "user_id"}, options={}
    )
    other = (
        add_entry(hass, unique_id="other@example.invalid", title="Other") if other_account else None
    )
    before_other = other.as_dict() if other else None
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": target.entry_id}, data=dict(target.data)
    )
    assert result["data_schema"]({"password": "new"})["username"] == target.unique_id
    if mfa:
        sdk.return_value.enable_channel99.side_effect = EzvizAuthVerificationCode()
    result = await manager.async_configure(
        result["flow_id"], {"username": target.unique_id, "password": "new"}
    )
    if mfa:
        sdk.return_value.enable_channel99.side_effect = None
        result = await manager.async_configure(result["flow_id"], {"sms_code": "123456"})
    assert result["reason"] == "reauth_successful"
    assert await TokenPersistence(hass, target).async_load() == TOKEN
    assert target.data == {"type": ATTR_TYPE_CLOUD, "url": REGION_URLS["eu"]}
    if other:
        assert other.as_dict() == before_other
    sdk.assert_called_with(
        account=target.unique_id, password="new", url=target.data["url"], timeout=DEFAULT_TIMEOUT
    )


@pytest.mark.parametrize("blank_codes", [False, True])
async def test_encryption_key_mfa_codes(hass, monkeypatch, blank_codes):
    """Blank optional codes map to None; supplied codes reach only the key fetch."""
    entry, client, _, result = await start_camera(hass, monkeypatch)
    client.get_cam_key.side_effect = [EzvizAuthVerificationCode(), "resolved-encryption"]
    manager = hass.config_entries.options
    result = await manager.async_configure(
        result["flow_id"], {**CAMERA, "enc_key": DEFAULT_FETCH_MY_KEY}
    )
    assert result["step_id"] == "camera_edit_2fa"
    result = await manager.async_configure(
        result["flow_id"], {} if blank_codes else {"cam_encryption_2fa_code": "654321"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    client.get_cam_key.assert_called_with("CAM", smscode=None if blank_codes else "654321")
    assert entry.options["cameras"]["CAM"]["enc_key"] == "resolved-encryption"
    assert set(entry.options["cameras"]["CAM"]) == set(CAMERA) - {"ephemeral_test_rtsp"}


async def test_reauth_cannot_change_account(hass, sdk):
    entry = add_entry(hass)
    before = entry.as_dict()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "other@example.invalid", "password": "new"}
        )
    sdk.assert_not_called()
    assert entry.as_dict() == before


@pytest.mark.parametrize(
    "host",
    [
        "ftp://example.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid/path",
        "example.invalid?token=secret",
        "example.invalid#fragment",
        "example.invalid:443",
        "example.invalid:bad",
        "bad host.invalid",
        "-bad.invalid",
        "bad_.invalid",
        "example..invalid",
        "a" * 254,
        "[broken",
        "example.invalid//",
    ],
)
async def test_strict_custom_host_recovery(hass, sdk, host):
    result = await start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER, "region": "custom", "url": host}
    )
    assert result["errors"] == {"base": "invalid_url"}
    sdk.assert_not_called()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == USER["username"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("https://[2001:db8::1]/", "[2001:db8::1]"),
        ("https://API.Example.Invalid/", "api.example.invalid"),
        ("https://bücher.example/", "xn--bcher-kva.example"),
    ],
)
async def test_valid_custom_host_forms(hass, sdk, host, expected):
    result = await start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER, "region": "custom", "url": host}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == expected
    assert result["result"].unique_id == USER["username"]


@pytest.mark.parametrize("timeout", [0, -1, 301, 1.5, "invalid", True, False])
@pytest.mark.parametrize("options", [False, True])
async def test_timeout_validation_and_recovery(hass, sdk, options, timeout):
    if options:
        entry = add_entry(hass)
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    else:
        manager = hass.config_entries.flow
        result = await start_user(hass)
    with pytest.raises(InvalidData):
        await manager.async_configure(
            result["flow_id"], {"timeout": timeout} if options else {**USER, "timeout": timeout}
        )
    sdk.assert_not_called()
    result = await manager.async_configure(
        result["flow_id"], {"timeout": 300} if options else {**USER, "timeout": 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    if not options:
        assert result["result"].unique_id == USER["username"]


@pytest.mark.parametrize("phase", ["select", "edit", "mfa"])
@pytest.mark.parametrize("reason", ["unloaded", "closing", "removed"])
async def test_camera_options_runtime_changes(hass, monkeypatch, phase, reason):
    entry, client, _, result = await start_camera(hass, monkeypatch)
    manager = hass.config_entries.options
    if phase == "mfa":
        client.get_cam_key.side_effect = EzvizAuthVerificationCode()
        result = await manager.async_configure(
            result["flow_id"], {**CAMERA, "enc_key": DEFAULT_FETCH_MY_KEY}
        )
        assert result["step_id"] == "camera_edit_2fa"
    if reason == "unloaded":
        del entry.runtime_data
    elif reason == "closing":
        entry.runtime_data.coordinator.shutting_down = True
    else:
        entry.runtime_data.coordinator.data = {}
    if phase == "select":
        manager.async_abort(result["flow_id"])
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(result["flow_id"], {"next_step_id": "camera_select"})
    else:
        result = await manager.async_configure(result["flow_id"], {} if phase == "mfa" else CAMERA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == ("no_cameras" if reason == "removed" else "integration_unavailable")
    # Cloud options remain editable even when the integration failed setup.
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    result = await manager.async_configure(result["flow_id"], {"timeout": 23})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["timeout"] == 23


@pytest.mark.parametrize("timeout", [0, -1, 301, None, True, "invalid"])
async def test_reauth_ignores_invalid_legacy_timeout(hass, sdk, timeout):
    entry = add_entry(hass, options={"timeout": timeout})
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data))
    result = await manager.async_configure(result["flow_id"], {"username": entry.unique_id, "password": "new"})
    assert result["reason"] == "reauth_successful"
    sdk.assert_called_once_with(account=entry.unique_id, password="new", url=entry.data["url"], timeout=DEFAULT_TIMEOUT)
