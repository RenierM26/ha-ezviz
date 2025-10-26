"""Tests for the EZVIZ Cloud custom config and options flow."""

from __future__ import annotations

from collections.abc import Generator
from importlib import import_module
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pyezvizapi.exceptions import EzvizAuthVerificationCode
import pytest

from config.custom_components.ezviz_cloud import config_flow
from config.custom_components.ezviz_cloud.const import (
    ATTR_TYPE_CLOUD,
    CONF_CAM_ENC_2FA_CODE,
    CONF_CAM_VERIFICATION_2FA_CODE,
    CONF_ENC_KEY,
    CONF_FFMPEG_ARGUMENTS,
    CONF_REGION,
    CONF_RF_SESSION_ID,
    CONF_RTSP_USES_VERIFICATION_CODE,
    CONF_SESSION_ID,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DEFAULT_FETCH_MY_KEY,
    DEFAULT_FFMPEG_ARGUMENTS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    OPTIONS_KEY_CAMERAS,
    REGION_CUSTOM,
    REGION_EU,
    REGION_URLS,
)
from homeassistant import loader
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from tests.common import MockConfigEntry

pytestmark = pytest.mark.parametrize(
    "ignore_translations_for_mock_domains", ["ezviz_cloud"]
)


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stop Home Assistant from setting up the integration during the flow."""
    with patch(
        "config.custom_components.ezviz_cloud.async_setup_entry",
        AsyncMock(return_value=True),
    ) as mock_setup:
        yield mock_setup


@pytest.fixture(autouse=True)
def register_ezviz_cloud_integration(hass: HomeAssistant) -> None:
    """Register the real custom integration with the loader."""
    base_path = Path("config/custom_components/ezviz_cloud")
    manifest = json.loads((base_path / "manifest.json").read_text())
    integration = loader.Integration(
        hass,
        "config.custom_components.ezviz_cloud",
        base_path,
        manifest,
        set(os.listdir(base_path)),
    )
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    hass.data[loader.DATA_COMPONENTS][DOMAIN] = import_module(
        "config.custom_components.ezviz_cloud"
    )


@pytest.fixture(autouse=True)
def mock_config_entries_setup(hass: HomeAssistant) -> Generator[AsyncMock]:
    """Prevent ConfigEntries from trying to load the real integration."""
    with patch.object(
        hass.config_entries,
        "async_setup",
        AsyncMock(return_value=True),
    ) as mock_setup:
        yield mock_setup


@pytest.fixture
def mock_ezviz_client() -> Generator[MagicMock]:
    """Patch the EzvizClient used by the config flow."""
    with patch(
        "config.custom_components.ezviz_cloud.config_flow.EzvizClient",
        autospec=True,
    ) as mock_client:
        instance = mock_client.return_value
        instance.login.return_value = {
            CONF_SESSION_ID: "sess-token",
            CONF_RF_SESSION_ID: "rf-token",
            "username": "cloud-user-id",
        }
        yield mock_client


def _mock_cloud_entry() -> MockConfigEntry:
    """Create a cloud config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        title="user@example.com",
        data={
            CONF_TYPE: ATTR_TYPE_CLOUD,
            CONF_SESSION_ID: "old-session",
            CONF_RF_SESSION_ID: "old-rf-session",
            CONF_URL: REGION_URLS[REGION_EU],
            CONF_USER_ID: "cloud-user-id",
        },
        options={CONF_TIMEOUT: DEFAULT_TIMEOUT, OPTIONS_KEY_CAMERAS: {}},
    )


def _attach_coordinator(hass: HomeAssistant, entry: MockConfigEntry, cameras: dict) -> SimpleNamespace:
    """Attach a fake coordinator for the options flow."""
    coordinator = SimpleNamespace(data=cameras, ezviz_client=MagicMock())
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_COORDINATOR: coordinator}
    return coordinator


async def test_user_flow_success(hass: HomeAssistant, mock_ezviz_client: MagicMock) -> None:
    """Test the happy-path user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "super-secret",
            CONF_REGION: REGION_EU,
            CONF_TIMEOUT: 45,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == {
        CONF_TYPE: ATTR_TYPE_CLOUD,
        CONF_SESSION_ID: "sess-token",
        CONF_RF_SESSION_ID: "rf-token",
        CONF_URL: REGION_URLS[REGION_EU],
        CONF_USER_ID: "cloud-user-id",
    }
    assert result["result"].unique_id == "user@example.com"
    assert result["result"].options == {CONF_TIMEOUT: 45, OPTIONS_KEY_CAMERAS: {}}

    mock_ezviz_client.assert_called_once_with(
        account="user@example.com",
        password="super-secret",
        url=REGION_URLS[REGION_EU],
        timeout=45,
    )


async def test_user_flow_invalid_custom_host(
    hass: HomeAssistant, mock_ezviz_client: MagicMock
) -> None:
    """Validate that an empty custom host errors before we try to log in."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "super-secret",
            CONF_REGION: REGION_CUSTOM,
            CONF_URL: "",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    errors = result["errors"] or {}
    assert errors.get("base") == "invalid_url"
    mock_ezviz_client.assert_not_called()


async def test_user_flow_with_mfa(
    hass: HomeAssistant, mock_ezviz_client: MagicMock
) -> None:
    """Verify we request SMS when Ezviz requires MFA."""
    mock_ezviz_client.return_value.login.side_effect = [
        EzvizAuthVerificationCode(),
        {
            CONF_SESSION_ID: "sess-token",
            CONF_RF_SESSION_ID: "rf-token",
            "username": "cloud-user-id",
        },
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "super-secret",
            CONF_REGION: REGION_EU,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_mfa_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sms_code": "123456"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SESSION_ID] == "sess-token"
    assert result["result"].options[CONF_TIMEOUT] == DEFAULT_TIMEOUT
    assert mock_ezviz_client.call_count == 2


async def test_reauth_flow_updates_tokens(
    hass: HomeAssistant, mock_ezviz_client: MagicMock
) -> None:
    """Ensure reauth success updates the config entry tokens."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)

    mock_ezviz_client.return_value.login.return_value = {
        CONF_SESSION_ID: "new-session",
        CONF_RF_SESSION_ID: "new-rf-session",
        "username": "cloud-user-id",
    }

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: entry.unique_id,
            CONF_PASSWORD: "fresh-pass",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SESSION_ID] == "new-session"
    assert entry.data[CONF_RF_SESSION_ID] == "new-rf-session"

    mock_ezviz_client.assert_called_once_with(
        account=entry.unique_id,
        password="fresh-pass",
        url=REGION_URLS[REGION_EU],
        timeout=DEFAULT_TIMEOUT,
    )


async def test_reauth_flow_with_mfa(
    hass: HomeAssistant, mock_ezviz_client: MagicMock
) -> None:
    """Ensure reauth handles a two-factor challenge."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)

    mock_ezviz_client.return_value.login.side_effect = [
        EzvizAuthVerificationCode(),
        {
            CONF_SESSION_ID: "reauth-session",
            CONF_RF_SESSION_ID: "reauth-rf",
            "username": "cloud-user-id",
        },
    ]

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: entry.unique_id,
            CONF_PASSWORD: "fresh-pass",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sms_code": "123456"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SESSION_ID] == "reauth-session"
    assert mock_ezviz_client.call_count == 2


async def test_options_flow_cloud_updates_timeout(hass: HomeAssistant) -> None:
    """Options flow should allow tweaking the global timeout."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)
    _attach_coordinator(hass, entry, cameras={})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TIMEOUT: 60},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_TIMEOUT: 60, OPTIONS_KEY_CAMERAS: {}}
    assert entry.options[CONF_TIMEOUT] == 60


async def test_options_flow_camera_select_no_devices(hass: HomeAssistant) -> None:
    """Abort camera selection when no coordinator data is available."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)
    _attach_coordinator(hass, entry, cameras={})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "camera_select"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_cameras"


async def test_options_flow_camera_edit_writes_credentials(hass: HomeAssistant) -> None:
    """Successful camera edit should persist per-camera credentials."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)
    cameras = {
        "C12345": {
            "name": "Porch",
            "local_ip": "192.0.2.10",
            "device_category": "IPC",
        }
    }
    _attach_coordinator(hass, entry, cameras=cameras)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "camera_select"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"serial": "C12345"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera_edit"

    resolved = {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "verification-code",
        CONF_ENC_KEY: "encryption-key",
        CONF_RTSP_USES_VERIFICATION_CODE: True,
        CONF_FFMPEG_ARGUMENTS: "/Streaming/Channels/101",
    }

    with patch.object(
        config_flow.EzvizOptionsFlowHandler,
        "_test_rtsp_credentials",
        AsyncMock(return_value=resolved),
    ) as mock_validator:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "admin",
                CONF_PASSWORD: DEFAULT_FETCH_MY_KEY,
                CONF_ENC_KEY: DEFAULT_FETCH_MY_KEY,
                CONF_RTSP_USES_VERIFICATION_CODE: True,
                "ephemeral_test_rtsp": False,
                CONF_FFMPEG_ARGUMENTS: DEFAULT_FFMPEG_ARGUMENTS,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPTIONS_KEY_CAMERAS]["C12345"] == {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "verification-code",
        CONF_ENC_KEY: "encryption-key",
        CONF_RTSP_USES_VERIFICATION_CODE: True,
        CONF_FFMPEG_ARGUMENTS: "/Streaming/Channels/101",
    }
    mock_validator.assert_awaited_once()


async def test_options_flow_camera_edit_requires_2fa(hass: HomeAssistant) -> None:
    """If EZVIZ asks for MFA we should branch into the 2FA step and resume."""
    entry = _mock_cloud_entry()
    entry.add_to_hass(hass)
    cameras = {
        "C12345": {
            "name": "Porch",
            "local_ip": "192.0.2.10",
            "device_category": "IPC",
        }
    }
    _attach_coordinator(hass, entry, cameras=cameras)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "camera_select"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"serial": "C12345"}
    )

    validator = AsyncMock(
        side_effect=[
            EzvizAuthVerificationCode(),
            {
                CONF_USERNAME: "admin",
                CONF_PASSWORD: "verification-code",
                CONF_ENC_KEY: "encryption-key",
                CONF_RTSP_USES_VERIFICATION_CODE: False,
            },
        ]
    )

    with patch.object(
        config_flow.EzvizOptionsFlowHandler,
        "_test_rtsp_credentials",
        validator,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "admin",
                CONF_PASSWORD: DEFAULT_FETCH_MY_KEY,
                CONF_ENC_KEY: DEFAULT_FETCH_MY_KEY,
                CONF_RTSP_USES_VERIFICATION_CODE: False,
                "ephemeral_test_rtsp": True,
                CONF_FFMPEG_ARGUMENTS: DEFAULT_FFMPEG_ARGUMENTS,
            },
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "camera_edit_2fa"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_CAM_VERIFICATION_2FA_CODE: "123456",
                CONF_CAM_ENC_2FA_CODE: "654321",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPTIONS_KEY_CAMERAS]["C12345"][CONF_PASSWORD] == "verification-code"
    assert validator.await_count == 2
