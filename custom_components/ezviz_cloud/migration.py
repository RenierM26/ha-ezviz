"""Unique ID migration utilities for ha-ezviz (exact-match, no regex).

Strategy:
- Build legacy unique_ids directly from coordinator data & entity description keys:
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


def _has_legacy_ids(
    entity_registry: er.EntityRegistry, platform_domain: str
) -> bool:
    """Return True if any entity for this platform still uses the legacy UID."""

    return any(
        reg_entry.platform == DOMAIN
        and reg_entry.domain == platform_domain
        and "." in reg_entry.unique_id
        for reg_entry in entity_registry.entities.values()
    )


def _build_legacy_map(
    devices_by_serial: Mapping[str, Mapping[str, Any]],
    allowed_keys: Iterable[str],
    rename_map: Mapping[str, str],
    presence_check: Callable[[str, Mapping[str, Any]], bool] | None,
) -> dict[str, tuple[str, str]]:
    """Return mapping of legacy UID -> (serial, effective_key)."""

    legacy_to_target: dict[str, tuple[str, str]] = {}
    allowed_key_set = set(allowed_keys)

    for serial, camera_data in devices_by_serial.items():
        name = camera_data.get("name")
        if not isinstance(name, str):
            continue

        def _should_include(key: str, data: Mapping[str, Any] = camera_data) -> bool:
            return not presence_check or presence_check(key, data)

        for key in allowed_key_set:
            if not _should_include(key):
                continue
            for variant in (key, key.lower(), key.upper()):
                legacy_to_target.setdefault(f"{serial}_{name}.{variant}", (serial, key))

        for old_key, new_key in rename_map.items():
            if new_key not in allowed_key_set or not _should_include(new_key):
                continue
            for variant in (old_key, old_key.lower(), old_key.upper()):
                legacy_to_target.setdefault(
                    f"{serial}_{name}.{variant}", (serial, new_key)
                )

    return legacy_to_target


def _create_or_clear_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform_domain: str,
    stats: MigrationStats,
    skipped_entity_ids: list[str],
) -> None:
    """Create or clear the Repairs issue describing migration skips."""

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
                "count_bad_format": str(stats.skipped_unmapped),
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


def _compute_change_for_entry(
    reg_entry: er.RegistryEntry,
    platform_domain: str,
    legacy_to_target: Mapping[str, tuple[str, str]],
    entity_registry: er.EntityRegistry,
    stats: MigrationStats,
    skipped_entity_ids: list[str],
) -> dict[str, Any] | None:
    """Return migration change dict for a single registry entry."""

    if reg_entry.platform != DOMAIN or reg_entry.domain != platform_domain:
        return None

    old_uid = reg_entry.unique_id
    stats.examined += 1

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
        return None

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


async def migrate_unique_ids_with_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EzvizDataUpdateCoordinator,
    *,
    platform_domain: str,  # "sensor", "binary_sensor", "switch"
    allowed_keys: Iterable[str],  # canonical descriptor keys
    key_renames: Mapping[str, str] | None = None,  # optional: old_key -> new_key
    presence_check: Callable[[str, Mapping[str, Any]], bool] | None = None,
) -> MigrationStats:
    """Migrate legacy unique_ids for one platform using exact matching.

    We migrate entries whose unique_id exactly equals:
        f"{serial}_{name}.{key}"   (case-sensitive)
    where `key` is from the platform's EntityDescription keys (allowed_keys).

    - If `presence_check` is provided, we only map keys for which
      `presence_check(key, camera_data)` is True.
    - If `presence_check` is None (default), we map **all** allowed keys
      (even if not currently present in coordinator data), which helps migrate
      legacy/orphaned entities reliably.
    """
    stats = MigrationStats()

    entity_registry = er.async_get(hass)
    devices_by_serial: dict[str, dict[str, Any]] = coordinator.data or {}
    rename_map = dict(key_renames or {})

    if not _has_legacy_ids(entity_registry, platform_domain):
        _LOGGER.debug("[%s] UID migration: no legacy IDs found", platform_domain)
        return stats

    legacy_to_target = _build_legacy_map(
        devices_by_serial, allowed_keys, rename_map, presence_check
    )
    skipped_entity_ids: list[str] = []

    def compute_change(reg_entry: er.RegistryEntry) -> dict[str, Any] | None:
        return _compute_change_for_entry(
            reg_entry,
            platform_domain,
            legacy_to_target,
            entity_registry,
            stats,
            skipped_entity_ids,
        )

    # Perform migration across all entities for this config entry
    await er.async_migrate_entries(hass, entry.entry_id, compute_change)

    _create_or_clear_issue(
        hass, entry, platform_domain, stats, skipped_entity_ids
    )

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
