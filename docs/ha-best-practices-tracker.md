# HA best-practices follow-up (PR236)

Scope: keep existing entity/device identities and supported HA 2026.10.4/2026.11.0.
No production configuration changes, history purges, merge or deployment.

- [x] Credential safety: write-only key text entity, no key restoration/state; images/proxy read options.
- [x] Unloaded options: cloud timeout remains editable; unavailable camera actions fail gracefully.
- [x] Migration recovery: translated exceptions, Repairs and retry; preserve partial progress.
- [x] Rename lifecycle: in-place entity-ID changes and actual registry-rename regression tests.
- [x] Input validation: strict API hosts; timeout 1–300 seconds, including reauth.
- [x] Typed runtime data: per-entry owner/coordinator/push on ConfigEntry.runtime_data.

Shared domain state may retain cleanup tombstones while non-cancellable workers finish,
because HA clears runtime_data after failed setup/unload. These are not active platform
lookups and must continue preventing overlapping sessions and credential recreation.

Credential migration does not erase historical recorder/restore records created by older
versions; purging existing user history is deliberately not part of this code update.

## Verification

- 194 native tests pass on **each** HA 2026.10.4 and 2026.11.0.
- Config flow: **338/338 statements, 54/54 branch outcomes** (100%); CI retains its gate.
- 52 acceptance tests and 44 standalone tests pass; Ruff and mypy pass.
- Local hassfest: zero invalid integrations, retaining only the existing explicitly
  documented custom-only SDK dependency exception.
- Actual state-machine tests verify empty credential states, options-based key rotation,
  real registry renames and surviving subscriptions. Repairs manager tests exercise the
  real migration retry API, disabled/blocked entries and migration-success/setup-retry.
- Per-entry platform/HTTP/media/diagnostics/options consumers no longer use nested
  hass.data runtime dictionaries. Cleanup ownership still survives HA unload/failure.

The credential text entity keeps its unique/entity ID and `text.set_value` action,
but its state is now always empty. Automations reading the old credential state need
to stop doing so; encryption credentials belong in camera options instead.
