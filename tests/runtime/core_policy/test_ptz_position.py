"""Point PTZ action validation, actual SDK payloads and owned HA worker lifetime."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import logging
from threading import Event, get_ident
from typing import Any
from unittest.mock import MagicMock

import custom_components
from custom_components import ezviz_cloud
from custom_components.ezviz_cloud.camera import EzvizCamera
from custom_components.ezviz_cloud.const import DOMAIN
from custom_components.ezviz_cloud.coordinator import EzvizDataUpdateCoordinator
from custom_components.ezviz_cloud.ptz import point_route
import probatio
from pyezvizapi import EzvizClient
from pyezvizapi.devices.snapshot import CameraState
from pyezvizapi.exceptions import PyEzvizError
import pytest
import requests

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.service import async_get_all_descriptions

from tests.runtime.test_hardening import entities


@pytest.fixture(autouse=True)
def integration(hass: HomeAssistant) -> None:
    """Resolve action metadata without account setup or network access."""
    loader.async_setup(hass)
    component = loader.Integration.resolve_from_root(hass, custom_components, DOMAIN)
    assert component is not None
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = component


def camera_target(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, serial: str = "CAM001"
) -> tuple[
    EzvizDataUpdateCoordinator, EzvizCamera, list[requests.PreparedRequest], list[int]
]:
    """Use a real SDK client/serializer and patch only transport acquisition."""
    _, _, coordinator, *_ = entities(hass)
    original = coordinator.data.pop("CAM001")
    coordinator.data[serial] = {
        **original,
        "alarm_notify": False,
        "supportExt": {"586": "1"},
        "resourceInfos": [{"resourceIdentifier": "Video", "localIndex": 0}],
    }
    snapshot = coordinator.inventory_snapshot
    coordinator.inventory_snapshot = replace(
        snapshot,
        devices={serial: CameraState(serial, "camera", coordinator.data[serial])},
        health={serial: snapshot.health["CAM001"]},
    )
    client = EzvizClient(
        token={"session_id": "synthetic", "api_url": "api.example.invalid"}
    )
    sent: list[requests.PreparedRequest] = []
    threads: list[int] = []

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        sent.append(request)
        threads.append(get_ident())
        assert kwargs["max_retries"] == 0
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"meta":{"code":200}}'
        return response

    monkeypatch.setattr(client, "_send_prepared", send)
    coordinator.ezviz_client = client
    camera = EzvizCamera(hass, coordinator, serial, "synthetic", "synthetic", "/stream")
    return coordinator, camera, sent, threads


async def add_camera(hass: HomeAssistant, camera: EzvizCamera) -> EntityPlatform:
    """Use native platform targeting/availability instead of a fake dispatcher."""
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="camera",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = camera.coordinator.config_entry
    await platform.async_add_entities([camera])
    return platform


async def test_action_registers_without_accounts_and_targets_real_camera(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only selected accounts/devices move; request runs off-loop with no stream."""
    assert await ezviz_cloud.async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, "ptz_control_coordinates")
    descriptions = await async_get_all_descriptions(hass)
    fields = descriptions[DOMAIN]["ptz_control_coordinates"]["fields"]
    assert set(fields) == {"x_axis", "y_axis", "lens"}
    selector = fields["lens"]["selector"]["select"]
    assert selector["options"] == ["auto", "1", "2"]
    assert selector["translation_key"] == "ptz_lens"
    assert selector["mode"] == "dropdown"
    assert not selector["custom_value"]
    first, cam1, sent1, threads1 = camera_target(hass, monkeypatch)
    second, cam2, sent2, _ = camera_target(hass, monkeypatch, "CAM002")
    platforms = []
    try:
        platforms = [await add_camera(hass, camera) for camera in (cam1, cam2)]
        before = deepcopy(first.data)
        await hass.services.async_call(
            DOMAIN,
            "ptz_control_coordinates",
            {"entity_id": cam1.entity_id, "x_axis": 0.25, "y_axis": 0.75},
            blocking=True,
        )
        assert len(sent1) == 1 and not sent2
        assert threads1[0] != get_ident()
        request = sent1[0]
        assert request.url.endswith("/CAM001/Video_1/1/PTZManualCtrl/CtrlPTZ3DPosition")
        assert isinstance(request.body, str)
        assert json.loads(request.body) == {
            "positionCtrlType": "point",
            "zoomType": "zoomIn",
            "positionPoint": {"x": 0.25, "y": 0.75},
            "positionRect": {"height": 1.0, "width": 1.0, "x": 0.0, "y": 0.0},
        }
        assert first.data == before
        assert cam1.stream is None and cam2.stream is None
        await hass.services.async_call(
            DOMAIN,
            "ptz_control_coordinates",
            {"entity_id": [cam1.entity_id, cam2.entity_id], "x_axis": 0, "y_axis": 1},
            blocking=True,
        )
        assert len(sent1) == 2 and len(sent2) == 1
        assert sent2[0].url.endswith(
            "/CAM002/Video_1/1/PTZManualCtrl/CtrlPTZ3DPosition"
        )
    finally:
        for platform in platforms:
            await platform.async_reset()
        await first.async_close()
        await second.async_close()


@pytest.mark.parametrize(
    "value", [True, False, -0.01, 1.01, float("nan"), float("inf"), None, "0.5"]
)
@pytest.mark.parametrize("axis", ["x_axis", "y_axis"])
async def test_invalid_service_coordinates_do_not_reach_sdk(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, value: object, axis: str
) -> None:
    """Native action schema rejects malformed inputs before any worker dispatch."""
    await ezviz_cloud.async_setup(hass, {})
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    platform = await add_camera(hass, camera)
    try:
        data = {
            "entity_id": camera.entity_id,
            "x_axis": 0.5,
            "y_axis": 0.5,
            axis: value,
        }
        with pytest.raises(probatio.Invalid):
            await hass.services.async_call(
                DOMAIN, "ptz_control_coordinates", data, blocking=True
            )
        assert not sent
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize(
    "value", ["", True, False, 1, 2, None, "3", "Video", "../Video", "1/2", [], {}]
)
async def test_invalid_service_lens_does_not_reach_sdk(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """The native action schema validates the dropdown before worker dispatch."""
    await ezviz_cloud.async_setup(hass, {})
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    platform = await add_camera(hass, camera)
    try:
        data = {
            "entity_id": camera.entity_id,
            "x_axis": 0.5,
            "y_axis": 0.5,
            "lens": value,
        }
        with pytest.raises(probatio.Invalid):
            await hass.services.async_call(
                DOMAIN, "ptz_control_coordinates", data, blocking=True
            )
        assert not sent
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("field", ["resource_identifier", "local_index"])
async def test_old_experimental_route_fields_are_not_silently_ignored(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """Old trial automation fields fail validation instead of moving another lens."""
    await ezviz_cloud.async_setup(hass, {})
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    platform = await add_camera(hass, camera)
    try:
        with pytest.raises(probatio.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "ptz_control_coordinates",
                {
                    "entity_id": camera.entity_id,
                    "x_axis": 0.5,
                    "y_axis": 0.5,
                    field: "1",
                },
                blocking=True,
            )
        assert not sent
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("flag", [None, False, True, 0, "0", 2, "2", 1.0, "false", []])
async def test_directional_ptz_is_not_proof_of_point_support(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, flag: object
) -> None:
    """Require exact app-backed 586/724 flags, not generic pan/tilt capabilities."""
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    coordinator.data["CAM001"]["supportExt"] = {
        "154": "1",
        "30": "1",
        "31": "1",
        "586": flag,
    }
    try:
        with pytest.raises(ServiceValidationError):
            await camera.async_ptz_control_coordinates(0.5, 0.5)
        assert not sent
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"supportExt": {"586": "01"}}, ("Video_1", "1")),
        ({"deviceInfos": {"supportExt": {"586": 1}}}, ("Video_1", "1")),
        (
            {
                "supportExt": {"724": "1"},
                "resourceInfos": [{"resourceIdentifier": "Video", "localIndex": 0}],
            },
            ("Video", "0"),
        ),
    ],
)
def test_verified_capability_and_route_forms(
    data: dict[str, Any], expected: tuple[str, str]
) -> None:
    """Honor nested capabilities and zero-based reported resources without guessing."""
    assert point_route(data) == expected


@pytest.mark.parametrize("lens", ["1", "2"])
async def test_lens_selection_uses_reported_channels_without_fallback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, lens: str
) -> None:
    """Metadata order, missing lenses and duplicate indices cannot redirect a move."""
    await ezviz_cloud.async_setup(hass, {})
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    platform = await add_camera(hass, camera)
    data = coordinator.data["CAM001"]
    data["supportExt"] = {"724": "1"}
    data["resourceInfos"] = [
        {"resourceIdentifier": "Video", "localIndex": 1},
        {"resourceIdentifier": "Video_1", "localIndex": "2"},
    ]
    call_data = {
        "entity_id": camera.entity_id,
        "x_axis": 0.25,
        "y_axis": 0.75,
        "lens": lens,
    }
    try:
        await hass.services.async_call(
            DOMAIN,
            "ptz_control_coordinates",
            {**call_data, "lens": "auto"},
            blocking=True,
        )
        assert sent[-1].url.endswith("/CAM001/Video/1/PTZManualCtrl/CtrlPTZ3DPosition")
        sent.clear()
        await hass.services.async_call(
            DOMAIN, "ptz_control_coordinates", call_data, blocking=True
        )
        assert len(sent) == 1
        resource = "Video" if lens == "1" else "Video_1"
        assert sent[-1].url.endswith(
            f"/CAM001/{resource}/{lens}/PTZManualCtrl/CtrlPTZ3DPosition"
        )
        assert json.loads(sent[-1].body)["positionPoint"] == {"x": 0.25, "y": 0.75}
        data["resourceInfos"].reverse()
        await hass.services.async_call(
            DOMAIN, "ptz_control_coordinates", call_data, blocking=True
        )
        assert len(sent) == 2
        assert sent[-1].url == sent[-2].url
        original = data["resourceInfos"]
        data["resourceInfos"] = [
            row for row in original if str(row["localIndex"]) != lens
        ]
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(
                DOMAIN, "ptz_control_coordinates", call_data, blocking=True
            )
        assert error.value.translation_key == "ptz_lens_unavailable"
        assert error.value.translation_placeholders == {"lens": lens}
        data["resourceInfos"] = [
            *original,
            {"resourceIdentifier": "Video_9", "localIndex": lens},
        ]
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(
                DOMAIN, "ptz_control_coordinates", call_data, blocking=True
            )
        assert error.value.translation_key == "ptz_lens_unavailable"
        assert len(sent) == 2
    finally:
        await platform.async_reset()
        await coordinator.async_close()


@pytest.mark.parametrize("index", [None, "../1", "1/action", True, -1, ""])
def test_malformed_reported_route_cannot_be_selected(index: object) -> None:
    """Unsafe or missing vendor indices fail before constructing a command URL."""
    with pytest.raises(ServiceValidationError) as error:
        point_route(
            {
                "supportExt": {"724": "1"},
                "resourceInfos": [{"resourceIdentifier": "Video", "localIndex": index}],
            },
            "1",
        )
    assert error.value.translation_key == "ptz_route_unknown"


@pytest.mark.parametrize("lens", ["auto", "1", "2"])
def test_direct_only_camera_keeps_fixed_app_route(lens: str) -> None:
    """586 never authorizes an arbitrary reported route or an extra lens."""
    data = {
        "supportExt": {"586": "1"},
        "resourceInfos": [{"resourceIdentifier": "Video_9", "localIndex": "2"}],
    }
    if lens == "2":
        with pytest.raises(ServiceValidationError) as error:
            point_route(data, lens)
        assert error.value.translation_key == "ptz_lens_unavailable"
    else:
        assert point_route(data, lens) == ("Video_1", "1")


@pytest.mark.parametrize("state", ["offline", "stale", "removed", "closing"])
async def test_unavailable_camera_never_sends_a_movement(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """Entity and coordinator lifetime fences apply before physical commands."""
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    if state == "offline":
        snapshot = coordinator.inventory_snapshot
        values = {**dict(snapshot.devices["CAM001"].values), "status": 0}
        coordinator.inventory_snapshot = replace(
            snapshot, devices={"CAM001": CameraState("CAM001", "camera", values)}
        )
    elif state == "stale":
        snapshot = coordinator.inventory_snapshot
        health = replace(snapshot.health["CAM001"], current=False)
        coordinator.inventory_snapshot = replace(snapshot, health={"CAM001": health})
    elif state == "removed":
        camera._removed = True
    else:
        coordinator.begin_shutdown()
    try:
        with pytest.raises(HomeAssistantError):
            await camera.async_ptz_control_coordinates(0.5, 0.5)
        assert not sent
    finally:
        await coordinator.async_close()


async def test_rejection_and_cancellation_preserve_owned_worker(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight point command remains owned until completion before HTTP close."""
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    api = coordinator.ezviz_client
    rejected = MagicMock(side_effect=PyEzvizError("synthetic rejection"))
    monkeypatch.setattr(api, "ptz_control_coordinates", rejected)
    try:
        with pytest.raises(HomeAssistantError):
            await camera.async_ptz_control_coordinates(0.5, 0.5)
        assert rejected.call_count == 1 and not sent
        started, release, finished = Event(), Event(), Event()
        closed = MagicMock()
        monkeypatch.setattr(api, "close_session", closed)

        def blocked(*_args: object, **_kwargs: object) -> bool:
            started.set()
            assert release.wait(5)
            finished.set()
            return True

        monkeypatch.setattr(api, "ptz_control_coordinates", blocked)
        task = asyncio.create_task(camera.async_ptz_control_coordinates(0.5, 0.5))
        assert await hass.async_add_executor_job(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        close_task = asyncio.create_task(coordinator.async_close())
        await asyncio.sleep(0)
        assert not finished.is_set() and not closed.called
        release.set()
        await close_task
        assert finished.is_set() and closed.call_count == 1
    finally:
        await coordinator.async_close()


@pytest.mark.parametrize("locate", [False, True])
async def test_direct_capability_does_not_enable_selected_resource_path(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, locate: bool
) -> None:
    """586 proves only the fixed direct route; explicit selection needs 724."""
    coordinator, camera, sent, _ = camera_target(hass, monkeypatch)
    coordinator.data["CAM001"]["supportExt"] = {"586": "1", "724": int(locate)}
    coordinator.data["CAM001"]["resourceInfos"] = [
        {"resourceIdentifier": "Video_1", "localIndex": "2"}
    ]
    try:
        if locate:
            await camera.async_ptz_control_coordinates(0.5, 0.5, "2")
            assert len(sent) == 1
            assert sent[0].url.endswith(
                "/CAM001/Video_1/2/PTZManualCtrl/CtrlPTZ3DPosition"
            )
        else:
            with pytest.raises(ServiceValidationError):
                await camera.async_ptz_control_coordinates(0.5, 0.5, "2")
            assert not sent
    finally:
        await coordinator.async_close()
