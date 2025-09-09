"""Unique ID migration utilities for ha-ezviz (exact-match, no regex).

Strategy:
- Build legacy unique_ids directly from coordinator data:
    legacy = f"{serial}_{name}.{key}"
- If an entity's unique_id equals that legacy string (case-sensitive), migrate to:
    new = f"{serial}_{key}"
- Tolerate past *key* case drift by also generating {key.lower()} and {key.upper()}
  legacy variants that map to the canonical new key.
- Optional key_renames: migrate "<serial>_<name>.<old_key>" → "<serial>_<new_key>".

Idempotent, no persistent stats; uses Repairs issues (translation_key) for skipped items.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import logging
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


@dataclass
class MigrationStats:
    """Counters for a migration pass (returned for tests/logging only)."""

    examined: int = 0
    migrated: int = 0
    skipped_unmapped: int = 0  # had a dot, but didn't match constructed legacy map
    skipped_collision: int = 0  # target UID already taken


async def migrate_unique_ids_with_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EzvizDataUpdateCoordinator,
    *,
    platform_domain: str,  # "sensor", "binary_sensor", "switch"
    allowed_keys: Iterable[str],  # valid entity description keys for this platform
    key_renames: Mapping[str, str] | None = None,  # optional: old_key -> new_key
    presence_check: Callable[[str, dict[str, Any]], bool] | None = None,
) -> MigrationStats:
    """Migrate legacy unique_ids for one platform using exact matching.

    We only migrate entries whose unique_id exactly equals:
        f"{serial}_{name}.{key}"   (case-sensitive)
    where:
        - serial, name come from coordinator.data[serial]["name"]
        - `presence_check(key, camera_data)` returns True (key actually applies)
    We also accept legacy key case variants (key.lower()/key.upper()) to avoid duplicates.

    For key renames, we migrate:
        f"{serial}_{name}.{old_key}" -> f"{serial}_{new_key}"
    (only if `presence_check(new_key, camera_data)` is True).
    """
    stats = MigrationStats()

    entity_registry = er.async_get(hass)
    devices_by_serial: dict[str, dict[str, Any]] = coordinator.data or {}
    allowed_key_set = set(allowed_keys)
    rename_map = dict(key_renames or {})

    if presence_check is None:
        # Default presence check: top-level key exists (good for sensor/binary_sensor)
        def presence_check(key: str, camera: dict[str, Any]) -> bool:
            return key in camera

    # Fast exit: nothing to scan if no entities with dot-UIDs for this platform
    if not any(
        reg_entry.platform == DOMAIN
        and reg_entry.domain == platform_domain
        and "." in reg_entry.unique_id
        for reg_entry in entity_registry.entities.values()
    ):
        _LOGGER.debug("[%s] UID migration: no legacy IDs found", platform_domain)
        return stats

    # Build a map of EXACT legacy -> (serial, effective_key)
    # effective_key is the post-rename key to use in the new unique_id.
    legacy_to_target: dict[str, tuple[str, str]] = {}

    for serial, camera_data in devices_by_serial.items():
        name = camera_data.get("name")
        if not isinstance(name, str):
            continue

        # Current keys (presence may be nested; e.g. switches)
        for key in allowed_key_set:
            if presence_check(key, camera_data):
                # tolerate past case variants of {key} to avoid duplicates
                for variant in (key, key.lower(), key.upper()):
                    legacy_to_target.setdefault(
                        f"{serial}_{name}.{variant}", (serial, key)
                    )

        # Renamed keys: allow migration from old_key to new_key (if new applies)
        for old_key, new_key in rename_map.items():
            if new_key in allowed_key_set and presence_check(new_key, camera_data):
                # accept case variants of the old key too
                for variant in (old_key, old_key.lower(), old_key.upper()):
                    legacy_to_target.setdefault(
                        f"{serial}_{name}.{variant}", (serial, new_key)
                    )

    skipped_entity_ids: list[str] = []

    def compute_change(reg_entry: er.RegistryEntry) -> dict[str, Any] | None:
        nonlocal stats

        # Only our integration + requested platform
        if reg_entry.platform != DOMAIN or reg_entry.domain != platform_domain:
            return None

        old_uid = reg_entry.unique_id
        stats.examined += 1

        # Only migrate legacy dot-form UIDs
        if "." not in old_uid:
            return None

        target = legacy_to_target.get(old_uid)
        if not target:
            stats.skipped_unmapped += 1
            skipped_entity_ids.append(reg_entry.entity_id)
            return None

        serial, effective_key = target
        new_uid = f"{serial}_{effective_key}"

        if new_uid == old_uid:
            return None  # already correct (unlikely for legacy)

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

    # Perform migration across all entities for this config entry
    await er.async_migrate_entries(hass, entry.entry_id, compute_change)

    # Repairs issue: create/update when there are skips; delete when clean
    issue_id = f"uid_migration_review_{platform_domain}_{entry.entry_id}"
    total_skipped = stats.skipped_unmapped + stats.skipped_collision

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
                "count_bad_format": str(stats.skipped_unmapped),  # treat as "unmapped"
                "count_unknown_serial": "0",
                "count_name_mismatch": "0",
                "count_key_not_allowed": "0",
                "count_key_not_present": "0",
                "count_collision": str(stats.skipped_collision),
                "examples": examples_text,
            },
        )
    else:
        async_delete_issue(hass, DOMAIN, issue_id)

    # Concise log
    log_fn = _LOGGER.info if stats.migrated else _LOGGER.debug
    log_fn(
        "[%s] UID migration (exact match): migrated=%s examined=%s (unmapped=%s collisions=%s)",
        platform_domain,
        stats.migrated,
        stats.examined,
        stats.skipped_unmapped,
        stats.skipped_collision,
    )

    return stats
