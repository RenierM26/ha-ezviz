"""Guided RTSP setup: mandatory LAN checks, explicit consent and preserved fields."""

import asyncio
import json
from threading import Event
from unittest.mock import MagicMock

from pyezvizapi.exceptions import AuthTestResultFailed, EzvizAuthVerificationCode, PyEzvizError
import pytest

from custom_components.ezviz_cloud import camera_flow, local, options_flow
from custom_components.ezviz_cloud.switch import EzvizRtspSwitch
from homeassistant.components.config.config_entries import OptionManagerFlowResourceView
from homeassistant.data_entry_flow import FlowResultType

from . import test_local as shared
from .test_local_rtsp import state

sdk = shared.sdk
flow_setup = shared.flow_setup
AUTH = local.CONF_LOCAL_CONTROL_AUTH
CAMERA = shared.CAMERA


@pytest.fixture
def probe(monkeypatch, flow_setup):
    _, api, _, _ = flow_setup
    api.get_detection_sensibility = MagicMock()
    factory = MagicMock()
    monkeypatch.setattr(camera_flow, "TestRTSPAuth", factory)
    return factory


async def submit(hass, entry, **changes):
    result = await shared.edit(hass, entry)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {**CAMERA, AUTH: "verification_code", **changes}
    )


@pytest.mark.parametrize("test_rtsp", [False, True])
async def test_lan_is_always_checked_before_optional_rtsp(hass, sdk, probe, flow_setup, test_rtsp):
    client, factory = sdk
    entry, _api, account, _ = flow_setup
    order = []
    client.services_switch_state.side_effect = lambda: (order.append("LAN"), state(1))[1]
    probe.return_value.main.side_effect = lambda: order.append("RTSP")
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=test_rtsp)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert order == (["LAN", "RTSP"] if test_rtsp else ["LAN"])
        assert factory.call_args.args[1] == CAMERA["password"]
        if test_rtsp:
            assert probe.call_args.args[2] == CAMERA["enc_key"]
        else:
            probe.assert_not_called()
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("test_rtsp", [False, True])
@pytest.mark.parametrize("failure,error", [("invalid", "local_control_invalid"), ("auth", "local_control_cannot_connect"), ("network", "local_control_cannot_connect"), ("unknown", "local_control_unsupported")])
async def test_lan_failure_preserves_choice_and_all_fields_without_rtsp_or_fallback(hass, sdk, probe, flow_setup, test_rtsp, failure, error):
    client, factory = sdk
    entry, api, account, _ = flow_setup
    changes = {"ephemeral_test_rtsp": test_rtsp, "rtsp_uses_verification_code": "verification_code"}
    if failure == "invalid":
        changes["password"] = ""
    elif failure == "unknown":
        client.services_switch_state.return_value = state(None)
    else:
        client.services_switch_state.side_effect = OSError("SECRET") if failure == "network" else PyEzvizError("SECRET")
    before = entry.options
    try:
        result = await submit(hass, entry, **changes)
        assert result["step_id"] == "camera_edit" and result["errors"] == {AUTH: error}
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "verification_code"
        assert defaults["ephemeral_test_rtsp"] is test_rtsp
        assert defaults["rtsp_uses_verification_code"] is True
        assert defaults["username"] == CAMERA["username"]
        assert defaults["password"] == changes.get("password", CAMERA["password"])
        assert defaults["enc_key"] == CAMERA["enc_key"] and "ffmpeg_arguments" not in defaults
        assert entry.options == before
        probe.assert_not_called()
        api.get_detection_sensibility.assert_not_called()
        client.services_switch_set.assert_not_called()
        if failure != "invalid":
            assert factory.call_count == 1 and factory.call_args.args[1] == CAMERA["password"]
        # Trying another source is a new explicit user choice, not an automatic fallback.
        client.services_switch_state.side_effect = None
        client.services_switch_state.return_value = state(1)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**defaults, AUTH: "encryption_key"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert factory.call_args.args[1] == CAMERA["enc_key"]
    finally:
        await account.async_close()


@pytest.mark.parametrize("test_rtsp", [False, True])
async def test_known_off_is_only_an_offer_when_rtsp_testing_was_requested(hass, sdk, probe, flow_setup, test_rtsp):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    before = entry.options
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=test_rtsp)
        probe.assert_not_called()
        client.services_switch_set.assert_not_called()
        if test_rtsp:
            assert result["step_id"] == "rtsp_enable"
            assert result["data_schema"]({}) == {"enable_rtsp": False}
            assert entry.options == before
            result = await hass.config_entries.options.async_configure(result["flow_id"])
            assert result["step_id"] == "rtsp_enable"
            client.services_switch_state.assert_called_once()
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": False})
            assert result["description"] == "rtsp_tested_without_enable"
            probe.return_value.main.assert_called_once()
            assert probe.call_args.args[2] == CAMERA["enc_key"]
        assert result["type"] is FlowResultType.CREATE_ENTRY
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure,error", [(AuthTestResultFailed("SECRET"), "rtsp_auth_failed"), (PyEzvizError("SECRET"), "cannot_connect"), (OSError("SECRET"), "cannot_connect")])
async def test_unchecked_enable_still_tests_and_reports_failure_with_fields_preserved(hass, sdk, probe, flow_setup, failure, error):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    before = entry.options
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True, rtsp_uses_verification_code="verification_code")
        probe.return_value.main.side_effect = failure
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": False})
        assert result["step_id"] == "camera_edit" and result["errors"] == {"base": error}
        probe.return_value.main.assert_called_once()
        assert probe.call_args.args[2] == CAMERA["password"]
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "verification_code" and defaults["ephemeral_test_rtsp"] is True
        assert defaults["password"] == CAMERA["password"] and defaults["enc_key"] == CAMERA["enc_key"]
        assert "ffmpeg_arguments" not in defaults and defaults["rtsp_uses_verification_code"] is True
        assert entry.options == before
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_off_with_lan_disabled_is_not_inferred_from_a_failed_rtsp_test(hass, sdk, probe, flow_setup):
    client, factory = sdk
    entry, _, account, _ = flow_setup
    probe.return_value.main.side_effect = AuthTestResultFailed("synthetic")
    try:
        result = await submit(hass, entry, **{AUTH: "disabled", "ephemeral_test_rtsp": True})
        assert result["step_id"] == "camera_edit" and result["errors"] == {"base": "rtsp_auth_failed"}
        factory.assert_not_called()
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("already_on", [False, True])
async def test_yes_enables_once_verifies_then_tests_and_saves(hass, sdk, probe, flow_setup, already_on):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    physical = False
    order = []

    def read():
        order.append("LAN")
        return state(int(physical))

    def write(_payload, *, rtsp):
        nonlocal physical
        physical = bool(rtsp)
        order.append("ENABLE")
        return type("Response", (), {"succeeded": True, "json": lambda self: {"statusCode": 1}})()

    client.services_switch_state.side_effect = read
    client.services_switch_set.side_effect = write
    probe.return_value.main.side_effect = lambda: order.append("RTSP")
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        assert result["step_id"] == "rtsp_enable" and order == ["LAN"]
        physical = already_on
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["description"] == "rtsp_enabled_and_tested"
        assert order == (["LAN", "LAN", "RTSP"] if already_on else ["LAN", "LAN", "ENABLE", "LAN", "RTSP"])
        assert client.services_switch_set.call_count == int(not already_on)
        stored = entry.options["cameras"]["CAM001"]
        assert stored[AUTH] == "verification_code" and stored["rtsp_uses_verification_code"] is False
        assert not any(key.startswith("ephemeral_") for key in stored)
        assert "enable_rtsp" not in stored
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure", ["refused", "network", "mismatch"])
async def test_unknown_write_outcome_returns_to_settings_without_retry_or_option_save(hass, sdk, probe, flow_setup, failure):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    before = entry.options
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        if failure == "refused":
            client.services_switch_set.return_value.succeeded = False
        elif failure == "network":
            client.services_switch_set.side_effect = OSError("synthetic")
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["step_id"] == "camera_edit" and result["errors"] == {"base": "rtsp_enable_unverified"}
        assert result["data_schema"]({})[AUTH] == "verification_code"
        assert result["data_schema"]({})["ephemeral_test_rtsp"] is True
        assert entry.options == before
        client.services_switch_set.assert_called_once()
        probe.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure", [AuthTestResultFailed("synthetic"), OSError("synthetic")])
async def test_enable_success_but_probe_failure_is_reported_separately(hass, sdk, probe, flow_setup, failure):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    before = entry.options
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        client.services_switch_state.side_effect = [state(0), state(1)]
        probe.return_value.main.side_effect = failure
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["errors"] == {"base": "rtsp_enabled_test_failed"}
        assert result["step_id"] == "camera_edit" and entry.options == before
        defaults = result["data_schema"]({})
        assert defaults[AUTH] == "verification_code" and defaults["ephemeral_test_rtsp"] is True
        client.services_switch_set.assert_called_once()
        # The next request re-reads on and tests only; there is no second enable.
        client.services_switch_state.side_effect = None
        client.services_switch_state.return_value = state(1)
        probe.return_value.main.side_effect = None
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**defaults, "rtsp_uses_verification_code": "verification_code"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert probe.call_args.args[2] == CAMERA["password"]
        client.services_switch_set.assert_called_once()
    finally:
        await account.async_close()


@pytest.mark.parametrize("queued", [False, True])
async def test_concurrent_edits_are_checked_inside_the_shared_lock_before_write(hass, sdk, probe, flow_setup, queued):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    entered, release = Event(), Event()
    blocker = pending = None

    def block(*_args):
        entered.set()
        assert release.wait(5)

    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        # An initial entry had no camera options. Change one of the fields the
        # candidate intends to save, while the offer or write is pending.
        if queued:
            blocker = asyncio.create_task(account.local_device("CAM001").async_execute(block, options={**CAMERA, AUTH: "verification_code"}))
            assert await asyncio.to_thread(entered.wait, 2)
            pending = asyncio.create_task(hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True}))
            await asyncio.sleep(0)
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, "enc_key": "NEW_KEY", AUTH: "encryption_key"}}})
        if queued:
            release.set()
            await blocker
            result = await pending
        else:
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["step_id"] == "camera_edit" and result["errors"] == {"base": "options_changed"}
        assert result["data_schema"]({})["enc_key"] == "NEW_KEY"
        client.services_switch_set.assert_not_called()
        probe.assert_not_called()
    finally:
        release.set()
        if blocker:
            await blocker
        if pending:
            await pending
        await account.async_close()


@pytest.mark.parametrize("enable", [False, True])
async def test_edit_during_post_enable_probe_does_not_overwrite_options(hass, sdk, probe, flow_setup, monkeypatch, enable):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    original = options_flow.EzvizOptionsFlowHandler._probe_rtsp

    async def probe_then_edit(handler, resolved):
        await original(handler, resolved)
        hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**CAMERA, "enc_key": "NEW_KEY"}}})

    monkeypatch.setattr(options_flow.EzvizOptionsFlowHandler, "_probe_rtsp", probe_then_edit)
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        client.services_switch_state.side_effect = [state(0), state(1)]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": enable})
        assert result["errors"] == {"base": "rtsp_enabled_options_changed" if enable else "options_changed"}
        assert entry.options["cameras"]["CAM001"]["enc_key"] == "NEW_KEY"
        assert client.services_switch_set.call_count == int(enable)
    finally:
        await account.async_close()


@pytest.mark.parametrize("failure,reason", [("runtime", "integration_unavailable"), ("shutdown", "integration_unavailable"), ("missing", "no_cameras")])
async def test_offer_is_invalidated_on_unload_or_missing_camera(hass, sdk, probe, flow_setup, failure, reason):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    try:
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        if failure == "runtime":
            del entry.runtime_data
        elif failure == "shutdown":
            account.begin_shutdown()
        else:
            account.data = {}
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        assert result["type"] is FlowResultType.ABORT and result["reason"] == reason
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


async def test_direct_step_without_a_pending_offer_cannot_write(hass, sdk, probe, flow_setup):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    try:
        result = await shared.edit(hass, entry)
        handler = hass.config_entries.options._progress[result["flow_id"]]
        result = await handler.async_step_rtsp_enable({"enable_rtsp": True})
        assert result["step_id"] == "camera_edit"
        client.services_switch_set.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("credential", [("verification_code", "password", "get_cam_auth_code", "cam_verification_2fa_code"), ("encryption_key", "enc_key", "get_cam_key", "cam_encryption_2fa_code")])
@pytest.mark.parametrize("lan_failure", [False, True])
async def test_mfa_also_validates_lan_first_and_preserves_requested_rtsp_test(hass, sdk, probe, flow_setup, credential, lan_failure):
    mode, field, method, code_field = credential
    client, factory = sdk
    entry, api, account, _ = flow_setup
    setattr(api, method, MagicMock(side_effect=[EzvizAuthVerificationCode(), "FETCHED_LOCAL_KEY"]))
    if lan_failure:
        client.services_switch_state.side_effect = OSError("synthetic")
    try:
        result = await submit(hass, entry, **{AUTH: mode, field: "fetch_my_key", "ephemeral_test_rtsp": True})
        assert result["step_id"] == "camera_edit_2fa"
        factory.assert_not_called()
        result = await hass.config_entries.options.async_configure(result["flow_id"], {code_field: "123456"})
        assert factory.call_args.args[1] == "FETCHED_LOCAL_KEY"
        probe.assert_not_called()
        client.services_switch_set.assert_not_called()
        if lan_failure:
            assert result["step_id"] == "camera_edit" and result["errors"] == {AUTH: "local_control_cannot_connect"}
            defaults = result["data_schema"]({})
            assert defaults[AUTH] == mode and defaults[field] == "FETCHED_LOCAL_KEY"
            assert defaults["ephemeral_test_rtsp"] is True
        else:
            assert result["step_id"] == "rtsp_enable"
            client.services_switch_state.side_effect = [state(0), state(1)]
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["description"] == "rtsp_enabled_and_tested"
            stored = entry.options["cameras"]["CAM001"]
            assert stored[field] == "FETCHED_LOCAL_KEY" and stored[AUTH] == mode
            assert code_field not in stored
    finally:
        await account.async_close()


async def test_native_dropdown_and_consent_form_http_serialization(hass, sdk, probe, flow_setup):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    view = OptionManagerFlowResourceView(hass.config_entries.options)
    try:
        result = await shared.edit(hass, entry)
        response = view.json(view._prepare_result_json(result))
        rendered = json.loads(response.body)
        fields = {field["name"]: field for field in rendered["data_schema"]}
        playback = fields["rtsp_uses_verification_code"]
        assert playback["selector"]["select"]["options"] == ["encryption_key", "verification_code"]
        assert playback["default"] == "encryption_key"
        assert fields[AUTH]["selector"]["select"]["mode"] == "dropdown"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**CAMERA, AUTH: "verification_code", "ephemeral_test_rtsp": True})
        rendered = json.loads(view.json(view._prepare_result_json(result)).body)
        assert rendered["step_id"] == "rtsp_enable"
        assert rendered["data_schema"][0]["default"] is False
        assert rendered["description_placeholders"] == {"serial": "CAM001"}
        client.services_switch_set.assert_not_called()
        probe.assert_not_called()
    finally:
        await account.async_close()


@pytest.mark.parametrize("probe_fails", [False, True])
async def test_flow_enable_updates_existing_switch_without_changed_options_or_reload(hass, sdk, probe, flow_setup, probe_fails):
    client, _ = sdk
    entry, _, account, reload = flow_setup
    saved = {key: value for key, value in CAMERA.items() if not key.startswith("ephemeral_")}
    saved[AUTH] = "verification_code"
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": saved}})
    rtsp = account.local_rtsp_coordinator("CAM001")
    assert account.local_rtsp_coordinator("CAM001") is rtsp
    entity = EzvizRtspSwitch(rtsp)
    try:
        await rtsp.async_refresh()
        assert entity.is_on is False
        listener = MagicMock()
        unsubscribe = rtsp.async_add_listener(listener)
        before = entry.options
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        client.services_switch_state.side_effect = [state(0), state(1)]
        if probe_fails:
            probe.return_value.main.side_effect = AuthTestResultFailed("synthetic")
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True})
        if probe_fails:
            assert result["step_id"] == "camera_edit" and result["errors"] == {"base": "rtsp_enabled_test_failed"}
        else:
            assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == before
        reload.assert_not_awaited()
        assert entity.is_on is True and entity.available
        listener.assert_called_once()
        unsubscribe()
    finally:
        await rtsp.async_shutdown()
        await account.async_close()


async def test_old_poll_queued_before_flow_enable_cannot_leave_switch_off(hass, sdk, probe, flow_setup):
    client, _ = sdk
    entry, _, account, _ = flow_setup
    saved = {key: value for key, value in CAMERA.items() if not key.startswith("ephemeral_")}
    hass.config_entries.async_update_entry(entry, options={"cameras": {"CAM001": {**saved, AUTH: "verification_code"}}})
    rtsp = account.local_rtsp_coordinator("CAM001")
    entered, release = Event(), Event()
    poll = submitted = None
    try:
        await rtsp.async_refresh()
        result = await submit(hass, entry, ephemeral_test_rtsp=True)
        reads = 0
        def read():
            nonlocal reads
            reads += 1
            if reads == 1:
                entered.set()
                assert release.wait(5)
            return state(int(reads == 3))
        client.services_switch_state.side_effect = read
        updates = []
        unsubscribe = rtsp.async_add_listener(lambda: updates.append(rtsp.data))
        poll = asyncio.create_task(rtsp.async_refresh())
        assert await asyncio.to_thread(entered.wait, 2)
        submitted = asyncio.create_task(hass.config_entries.options.async_configure(result["flow_id"], {"enable_rtsp": True}))
        await asyncio.sleep(0)
        release.set()
        await poll
        result = await submitted
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert rtsp.data is True and updates[-1] is True
        assert reads == 3
        client.services_switch_set.assert_called_once()
        unsubscribe()
    finally:
        release.set()
        if poll:
            await poll
        if submitted:
            await submitted
        await account.async_close()
