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

- 269 native tests pass on **each** HA 2026.10.4 and 2026.11.0.
- Config flow: **341/341 statements, 54/54 branch outcomes** (100%); CI retains its gate.
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

## Reliability follow-up

- [x] Acknowledged encryption-key changes/fetches keep ownership through option commit
  and image invalidation, even after the requesting task is cancelled or unload starts.
  A per-camera lock makes queued changes read the latest committed old key.
- [x] Normal and MFA camera edits use one fresh-read options patch helper. Unrelated
  changes survive; changed fields on the edited camera show a review/retry form.
- [x] Cleanup success is explicit, not inferred from task completion. Failed stages
  retain ownership and can retry; token drains still run after earlier failures.
  HTTP cannot close before push stops. Setup cannot replace an unresolved owner.
  Removal retains the owner until cleanup/credential removal finish.
- [x] Typed camera/image media identifier parsers map malformed identifiers, bad URLs,
  invalid limits (allowed range 1–200) and SDK failures to HA domain errors. Signed URLs
  and existing proxied/legacy image identifiers remain supported.

Refactoring is deliberately limited to `credentials.py` (fresh patching plus owned
per-camera credential operations), shared config-flow completion, explicit cleanup
outcomes and media parsing. No broader coordinator/MQTT worker rewrite is included.

Additional evidence includes actual HA stop/disk restart after a cancelled key change,
queued rotations, removal during an in-flight change without resurrecting credentials,
cleanup failure/retry and media error/recovery controls. The standalone shutdown test
now waits for the executor-start boundary after checking bounded unload, avoiding a
scheduler-dependent assertion without weakening the unload bound.
