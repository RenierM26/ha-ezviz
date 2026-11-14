# Change history

## Unreleased — main

Not a published integration version. Manifest `0.2.0.25` is still the older
published version number; these later changes require a new version/tag before
release. Historical published notes remain on [GitHub Releases](https://github.com/RenierM26/ha-ezviz/releases).

### Upgrade considerations

- Requires HA 2026.10.4+, Python 3.14.2+ and published SDK 2.2.0+.
  Built-in/custom EZVIZ coexistence has an unresolved SDK-pin conflict.
- Encryption-key text entities are write-only. IDs and `text.set_value` remain;
  state is empty, not restored, and cannot supply an automation's secret. Existing
  recorder/history/backups are not purged. Use camera options for configuration.
- Version-5 account metadata moves the complete SDK token into one private durable
  snapshot. Preserve the HA config volume and a coherent pre-upgrade backup;
  source-only downgrade is not promised to reverse migrations.

### Integration and reliability

- Shared typed SDK inventory/freshness for cameras, detectors, bulbs and plugs;
  retain IDs and stale-row health instead of inventing online/capability semantics.
- Optional channel-99 push independent of polling, stable persisted installation
  identity, durable token acknowledgement and retained shutdown/credential ownership.
- Camera capture/snapshot handling, timer/stream registry-rename lifetime fixes,
  scoped push device lookup, safe translated HTTP/API error categories and published
  SDK primary-error preservation through switch fallback.
- Cached allowlisted diagnostics, native HA producer/restoration/storage controls,
  strict Core policy and isolated test layers. Detector binary semantics and live
  device/push/browser qualification remain explicitly limited.

### Repository maintenance

- Shared development/editor/CI checks; audited ownership and modular flow/state
  boundaries; behavioral recovery tests.
- Current setup, support, migration and release guides; immutable validation pins,
  bare-version tag validation and a read-only source/metadata release preflight.

See the [overhaul tracker](docs/overhaul-backlog.md) for merged/in-review status.
