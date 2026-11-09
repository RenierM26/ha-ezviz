"""Actionable migration issues; never guess account ownership or camera serials."""

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


@callback
def create_migration_issue(hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
    """Give the user corrective instructions and a safe explicit retry."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"migration_{entry.entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=key,
        translation_placeholders={"title": entry.title},
        data={"entry_id": entry.entry_id},
    )


@callback
def clear_migration_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"migration_{entry.entry_id}")


class MigrationRepairFlow(RepairsFlow):
    """Retry only the entry referenced by this issue, after the user fixes it."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        errors = {}
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if entry is None:
                return self.async_create_entry(data={})
            if entry.disabled_by:
                errors["base"] = "entry_disabled"
            elif entry.state is ConfigEntryState.MIGRATION_ERROR:
                try:
                    await self.hass.config_entries.async_retry_migration(self.entry_id)
                except HomeAssistantError:
                    errors["base"] = "retry_failed"
                else:
                    if ir.async_get(self.hass).async_get_issue(DOMAIN, f"migration_{self.entry_id}") is not None:
                        errors["base"] = "retry_failed"
                    else:
                        return self.async_create_entry(data={})
            else:
                errors["base"] = "retry_failed"
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), errors=errors)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    if (
        not issue_id.startswith("migration_")
        or data is None
        or not isinstance(data.get("entry_id"), str)
    ):
        raise ValueError("Unknown migration issue")
    return MigrationRepairFlow(data["entry_id"])
