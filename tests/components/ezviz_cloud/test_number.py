"""Tests for EZVIZ number controls."""

from __future__ import annotations

from unittest.mock import MagicMock

from config.custom_components.ezviz_cloud.number import (
    _alarm_volume_getter,
    _alarm_volume_setter,
)


def test_alarm_volume_reads_and_preserves_chime_payload() -> None:
    """Expose ChimeMusic volume and retain the remaining device config."""
    camera_data = {
        "channelNo": 2,
        "STATUS": {
            "optionals": {
                "ChimeMusic": {"enabled": True, "musicId": 3, "volume": 25}
            }
        },
    }
    client = MagicMock()

    assert _alarm_volume_getter(camera_data) == 25

    _alarm_volume_setter()(client, "CAMERA123", 101, camera_data)

    client.set_dev_config_kv.assert_called_once_with(
        "CAMERA123",
        2,
        "ChimeMusic",
        {"enabled": True, "musicId": 3, "volume": 100},
    )
