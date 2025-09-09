"""Entity-registry unique_id migration utilities for ha-ezviz.

Migrates legacy unique_ids that embed the camera name to a stable format:

    "<SERIAL>_<CAMERA NAME>.<KEY>"  →  "<SERIAL>_<KEY>"

Design:
- Idempotent and cheap; safe to run each setup.
- Strict camera-name verification (normalized).
- No persistence of transient stats/options.
- Uses Repairs issues (translation_key) to inform the user of skipped items.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .const import DOMAIN
from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Migrate ONLY name-bearing legacy UIDs:
#   "<SERIAL>_<CAMERA NAME>.<KEY>"  →  "{serial}_{key}"
LEGACY_UID_REGEX = re.compile(
    r"^(?P<serial>[A-Za-z0-9]+)_(?P<camera_name>.+)\.(?P<key>[^.]+)$"
)


@dataclass
class MigrationStats:
    """Counters for a migration pass (returned for tests/logging only)."""

    examined: int = 0
    migrated: int = 0
    skipped_bad_format: int = 0  # doesn't match "<serial>_<name>.<key>"
    skipped_unknown_serial: int = 0  # serial not in coordinator.data
    skipped_name_mismatch: int = 0  # camera name in UID != coordinator name
    skipped_key_not_allowed: int = 0  # key not in allowlist
    skipped_key_not_present: int = 0  # key not present in camera data
    skipped_collision: int = 0  # target UID already taken


def _normalize_name(value: str | None) -> str | None:
    """Normalize names for comparison (case/whitespace-insensitive)."""
    if not value:
        return None
    return " ".join(value.strip().lower().split())


async def migrate_unique_ids_with_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EzvizDataUpdateCoordinator,
    *,
    platform_domain: str,  # "sensor", "binary_sensor", "switch"
    allowed_keys: Iterable[str],  # valid entity description keys for this platform
    key_renames: Mapping[str, str] | None = None,  # optional: map old_key -> new_key
) -> MigrationStats:
    """Migrate legacy unique_ids for one platform using coordinator data.

    Rules:
      - UID must match "<serial>_<camera name>.<key>".
      - Serial must exist in coordinator.data.
      - Camera name in UID must equal coordinator name (normalized).
      - Key must be in `allowed_keys` AND present in that camera's data.
      - Renames via `key_renames` are applied before validation.
      - Disabled entities are included; we do not change their disabled state.
      - Skips are summarized in a Repairs issue (with translation_key).
    """
    stats = MigrationStats()

    entity_registry = er.async_get(hass)
    devices_by_serial: dict[str, dict[str, Any]] = coordinator.data or {}

    allowed_key_set = set(allowed_keys)
    rename_map = dict(key_renames or {})

    # Fast exit: nothing to scan for this platform if no entities have legacy (dot) UIDs
    if not any(
        reg_entry.platform == DOMAIN
        and reg_entry.domain == platform_domain
        and "." in reg_entry.unique_id
        for reg_entry in entity_registry.entities.values()
    ):
        _LOGGER.debug("[%s] UID migration: no legacy IDs found", platform_domain)
        return stats

    skipped_entity_ids: list[str] = []

    def compute_change(reg_entry: er.RegistryEntry) -> dict[str, Any] | None:
        nonlocal stats

        # Only act on our integration + requested platform
        if reg_entry.platform != DOMAIN or reg_entry.domain != platform_domain:
            return None

        old_uid = reg_entry.unique_id
        stats.examined += 1

        # Already in new scheme (no dot) → nothing to do
        if "." not in old_uid:
            return None

        match = LEGACY_UID_REGEX.match(old_uid)
        if not match:
            stats.skipped_bad_format += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        serial_in_uid = match.group("serial")
        name_in_uid = match.group("camera_name")
        key_in_uid = match.group("key")

        camera_data = devices_by_serial.get(serial_in_uid)
        if camera_data is None:
            stats.skipped_unknown_serial += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        # Strict camera-name check
        coordinator_name_norm = _normalize_name(camera_data.get("name"))
        uid_name_norm = _normalize_name(name_in_uid)
        if (
            not coordinator_name_norm
            or not uid_name_norm
            or uid_name_norm != coordinator_name_norm
        ):
            stats.skipped_name_mismatch += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        effective_key = rename_map.get(key_in_uid, key_in_uid)

        if effective_key not in allowed_key_set:
            stats.skipped_key_not_allowed += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        if effective_key not in camera_data:
            stats.skipped_key_not_present += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        new_uid = f"{serial_in_uid}_{effective_key}"
        if new_uid == old_uid:
            return None  # nothing to change

        # Avoid collisions
        existing_entity_id = entity_registry.async_get_entity_id(
            reg_entry.domain, reg_entry.platform, new_uid
        )
        if existing_entity_id and existing_entity_id != reg_entry.entity_id:
            stats.skipped_collision += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            _LOGGER.warning(
                "Unique ID migration collision for %s: %s -> %s (already used by %s)",
                reg_entry.entity_id,
                old_uid,
                new_uid,
                existing_entity_id,
            )
            return None

        _LOGGER.debug(
            "Migrating unique_id for %s: %s -> %s",
            reg_entry.entity_id,
            old_uid,
            new_uid,
        )
        stats.migrated += 1
        return {"new_unique_id": new_uid}

    # Perform migration across all entities in the config entry
    await er.async_migrate_entries(hass, entry.entry_id, compute_change)

    # Repairs issue: create/update when there are skips; delete when clean
    issue_id = f"uid_migration_review_{platform_domain}_{entry.entry_id}"
    total_skipped = (
        stats.skipped_bad_format
        + stats.skipped_unknown_serial
        + stats.skipped_name_mismatch
        + stats.skipped_key_not_allowed
        + stats.skipped_key_not_present
        + stats.skipped_collision
    )

    if total_skipped:
        sample_entity_ids = sorted(set(skipped_entity_ids))[:20]
        examples_text = "\n".join(f"- {entity_id}" for entity_id in sample_entity_ids)

        async_create_issue(
            hass=hass,
            domain=DOMAIN,
            issue_id=issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="uid_migration_review",
            translation_placeholders={
                "platform": platform_domain,
                "examined": str(stats.examined),
                "migrated": str(stats.migrated),
                "count_bad_format": str(stats.skipped_bad_format),
                "count_unknown_serial": str(stats.skipped_unknown_serial),
                "count_name_mismatch": str(stats.skipped_name_mismatch),
                "count_key_not_allowed": str(stats.skipped_key_not_allowed),
                "count_key_not_present": str(stats.skipped_key_not_present),
                "count_collision": str(stats.skipped_collision),
                "examples": examples_text,
            },
        )
    else:
        async_delete_issue(hass, DOMAIN, issue_id)

    # Concise summary
    log_fn = _LOGGER.info if stats.migrated else _LOGGER.debug
    log_fn(
        "[%s] UID migration: migrated=%s examined=%s "
        "(bad_format=%s unknown_serial=%s name_mismatch=%s key_not_allowed=%s key_not_present=%s collisions=%s)",
        platform_domain,
        stats.migrated,
        stats.examined,
        stats.skipped_bad_format,
        stats.skipped_unknown_serial,
        stats.skipped_name_mismatch,
        stats.skipped_key_not_allowed,
        stats.skipped_key_not_present,
        stats.skipped_collision,
    )

    return stats
