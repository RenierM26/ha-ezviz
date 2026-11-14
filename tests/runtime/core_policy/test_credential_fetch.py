"""Cloud credential-query failures keep partial results without changing saved keys."""

from copy import deepcopy
from unittest.mock import MagicMock

from pyezvizapi.exceptions import (
    AuthTestResultFailed,
    DeviceException,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidURL,
    PyEzvizError,
)
import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.runtime import test_config_flow as controls

integration = controls.integration


async def start_saved_camera(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    password: str | None = "saved-sticker",
) -> tuple[ConfigEntry, MagicMock, dict]:
    """Select the camera after seeding existing credentials through entry options."""
    entry, client, _rtsp, previous = await controls.start_camera(hass, monkeypatch)
    options = deepcopy(dict(entry.options))
    options["cameras"]["CAM"] = {
        **controls.CAMERA,
        "password": password,
        "enc_key": "saved-encryption",
        "rtsp_uses_verification_code": True,
    }
    options["cameras"]["CAM"].pop("ephemeral_test_rtsp")
    hass.config_entries.async_update_entry(entry, options=options)
    manager = hass.config_entries.options
    manager.async_abort(previous["flow_id"])
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "camera_select"}
    )
    result = await manager.async_configure(result["flow_id"], {"serial": "CAM"})
    return entry, client, result


@pytest.mark.parametrize("mfa", [False, True])
@pytest.mark.parametrize("field", ["password", "enc_key"])
@pytest.mark.parametrize(
    "error_type",
    [DeviceException, PyEzvizError, InvalidURL, HTTPError, AuthTestResultFailed],
)
async def test_failed_query_identifies_only_its_field_and_preserves_saved_credentials(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mfa: bool,
    field: str,
    error_type: type[PyEzvizError],
) -> None:
    """Error 2009 on sticker retrieval must not imply that a successful key fetch failed."""
    entry, client, result = await start_saved_camera(hass, monkeypatch)
    manager = hass.config_entries.options
    before = deepcopy(dict(entry.options))
    payload = {
        **controls.CAMERA,
        "password": "fetch_my_key",
        "enc_key": "fetch_my_key",
        "rtsp_uses_verification_code": True,
    }
    if mfa:
        client.get_cam_key.side_effect = EzvizAuthVerificationCode()
        result = await manager.async_configure(result["flow_id"], payload)
        assert result["step_id"] == "camera_edit_2fa"
        client.get_cam_key.side_effect = None
    failed = client.get_cam_auth_code if field == "password" else client.get_cam_key
    failed.side_effect = error_type(
        "2009 Device network abnormal; PRIVATE_RESPONSE_SECRET"
    )
    submission = (
        {"cam_encryption_2fa_code": "654321", "cam_verification_2fa_code": "123456"}
        if mfa
        else payload
    )
    result = await manager.async_configure(result["flow_id"], submission)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera_edit"
    assert result["errors"] == {
        field: "verification_code_fetch_failed"
        if field == "password"
        else "encryption_key_fetch_failed"
    }
    assert dict(entry.options) == before
    prefill = result["data_schema"]({})
    assert prefill[field] == before["cameras"]["CAM"][field]
    assert prefill["rtsp_uses_verification_code"]
    if field == "password":
        assert prefill["enc_key"] == "fetched-encryption"
    else:
        client.get_cam_auth_code.assert_not_called()
    label = "verification/sticker code" if field == "password" else "encryption key"
    assert f"EZVIZ {label} retrieval failed" in caplog.text
    assert "PRIVATE_RESPONSE_SECRET" not in caplog.text
    assert "account permission" not in caplog.text
    # Keeping the current verification credential saves the new ENC key only
    # after explicit submission, without another failed sticker-code request.
    if field == "password":
        key_calls = client.get_cam_key.call_count
        sticker_calls = client.get_cam_auth_code.call_count
        result = await manager.async_configure(result["flow_id"], prefill)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["cameras"]["CAM"]["enc_key"] == "fetched-encryption"
        assert entry.options["cameras"]["CAM"]["password"] == "saved-sticker"
        assert entry.options["cameras"]["OTHER"] == before["cameras"]["OTHER"]
        assert client.get_cam_auth_code.call_count == sticker_calls
        assert client.get_cam_key.call_count == key_calls


@pytest.mark.parametrize("saved", [None, "", "fetch_my_key"])
async def test_absent_verification_credential_does_not_invent_a_fallback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, saved: str | None
) -> None:
    """A failed retrieval without a usable saved credential remains an explicit retry."""
    entry, client, result = await start_saved_camera(hass, monkeypatch, saved)
    before = deepcopy(dict(entry.options))
    client.get_cam_auth_code.side_effect = PyEzvizError("2009")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**controls.CAMERA, "password": "fetch_my_key", "enc_key": "fetch_my_key"},
    )
    assert result["errors"] == {"password": "verification_code_fetch_failed"}
    defaults = result["data_schema"]({})
    assert defaults["password"] == "fetch_my_key"
    assert defaults["enc_key"] == "fetched-encryption"
    assert dict(entry.options) == before


@pytest.mark.parametrize("mfa", [False, True])
async def test_fetch_only_encryption_key_never_queries_existing_verification_code(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mfa: bool
) -> None:
    """A cloud sticker-code failure must not block an independent key-only update."""
    entry, client, result = await start_saved_camera(hass, monkeypatch)
    manager = hass.config_entries.options
    client.get_cam_auth_code.side_effect = PyEzvizError("2009")
    if mfa:
        client.get_cam_key.side_effect = EzvizAuthVerificationCode()
    result = await manager.async_configure(
        result["flow_id"],
        {
            **controls.CAMERA,
            "password": "saved-sticker",
            "enc_key": "fetch_my_key",
            "rtsp_uses_verification_code": True,
        },
    )
    if mfa:
        assert result["step_id"] == "camera_edit_2fa"
        client.get_cam_key.side_effect = None
        result = await manager.async_configure(
            result["flow_id"], {"cam_encryption_2fa_code": "654321"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cameras"]["CAM"]["enc_key"] == "fetched-encryption"
    assert entry.options["cameras"]["CAM"]["password"] == "saved-sticker"
    assert entry.options["cameras"]["CAM"]["rtsp_uses_verification_code"]
    client.get_cam_auth_code.assert_not_called()
