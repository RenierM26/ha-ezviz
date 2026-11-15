"""Shared observed-only intercom setting construction."""

from collections.abc import Callable
from functools import partial
from typing import Any

from pyezvizapi.devices.audio import volume_config
from pyezvizapi.devices.intercom import ChimeField, chime_value, set_intercom_chime

from .entity import EzvizEntity
from .setting_commands import _write_config
from .setting_writes import SettingCommand


def _project_chime(data: dict[str, Any], field: ChimeField, value: bool | int) -> None:
    """Keep accepted siblings for commands only; never resurrect missing fields."""
    if chime_value(data, field) is None:
        return
    config = volume_config(data, "ChimeMusic")
    assert config is not None
    config[field] = int(value)
    _write_config(data, "ChimeMusic", config)


def prepare_chime(
    entity: EzvizEntity, field: ChimeField, value: bool | int
) -> SettingCommand:
    """Read detached observations and accepted siblings under the account lock."""
    data = entity.coordinator.settings.command_data(entity.serial, entity.data)
    projections: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
        (f"ChimeMusic:{field}", partial(_project_chime, field=field, value=value)),
    )
    return SettingCommand(
        set_intercom_chime,
        (entity.coordinator.ezviz_client, entity.serial, field, value, data),
        lambda: projections,
    )
