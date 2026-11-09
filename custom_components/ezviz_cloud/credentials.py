"""Owned per-camera credential operations and conflict-safe option patches."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_ENC_KEY, OPTIONS_KEY_CAMERAS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EzvizDataUpdateCoordinator
    from .runtime import EzvizConfigEntry

_MISSING = object()


class CameraOptionsConflict(HomeAssistantError):
    """The camera changed while the user was editing or validating credentials."""


def camera_options(entry: EzvizConfigEntry, serial: str) -> dict[str, Any]:
    """Detach the baseline so another update cannot mutate it in place."""
    return deepcopy(dict((entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(serial, {})))


@callback
def build_camera_options(
    entry: EzvizConfigEntry,
    serial: str,
    patch: Mapping[str, Any],
    *,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge into the latest entry; no await between checking and committing.

    Unrelated cameras, cloud settings and unknown camera fields are preserved.
    Edited fields changed since the form opened require explicit user review.
    """
    options = dict(entry.options)
    cameras = dict(options.get(OPTIONS_KEY_CAMERAS, {}) or {})
    current = dict(cameras.get(serial, {}))
    if expected is not None and any(
        current.get(key, _MISSING) != expected.get(key, _MISSING) for key in patch
    ):
        raise CameraOptionsConflict("Camera settings changed; review the current values")
    cameras[serial] = {**current, **patch}
    return {**options, OPTIONS_KEY_CAMERAS: cameras}


class CameraCredentials:
    """Serialize device key fetch/change and the resulting local commit together."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EzvizConfigEntry | None,
        coordinator: EzvizDataUpdateCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._locks: dict[str, asyncio.Lock] = {}

    def key(self, serial: str) -> str | None:
        if self.entry is None:
            return None
        return camera_options(self.entry, serial).get(CONF_ENC_KEY)

    @callback
    def commit(self, serial: str, key: str) -> None:
        """Commit after device success, even during unload; never resurrect removal."""
        entry = self.entry
        if entry is None or self.hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            return
        options = build_camera_options(entry, serial, {CONF_ENC_KEY: key})
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.coordinator.async_update_listeners()

    async def async_change(self, serial: str, new_key: str) -> str:
        return await self._async_update(serial, new_key)

    async def async_fetch(self, serial: str) -> str:
        return await self._async_update(serial, None)

    async def _async_update(self, serial: str, new_key: str | None) -> str:
        async def operation() -> str:
            async with self._locks.setdefault(serial, asyncio.Lock()):
                if self.coordinator.shutting_down:
                    raise HomeAssistantError("EZVIZ coordinator is shutting down")
                client = self.coordinator.ezviz_client
                # Read old key inside the lock: queued changes must use the preceding result.
                old_key = self.key(serial)
                if new_key is None:
                    key = await self.hass.async_add_executor_job(client.get_cam_key, serial)
                else:
                    await self.hass.async_add_executor_job(
                        lambda: client.set_video_enc(
                            serial=serial, enable=2, new_password=new_key, old_password=old_key
                        )
                    )
                    key = new_key
                self.commit(serial, key)
                return key

        # The owner includes lock wait, SDK operation, option commit and invalidation.
        # Shielding only the SDK future would lose the commit when the waiter cancels.
        return await self.coordinator.async_execute_owned(operation)
