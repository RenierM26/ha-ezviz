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

- 305 native tests pass on **each** HA 2026.10.4 and 2026.11.0.
- Config flow: **367/367 statements, 54/54 branch outcomes** (100%); CI retains its gate.
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

## Startup token-identity regression

- [x] SDK constructor auth failures now request translated HA reauthentication before
  any client runtime is allocated, rather than escaping as unexpected setup errors.
- [x] Real SDK/HA tests cover changed/missing identity in entry/private tokens,
  fresh reauth replacing stale state, real storage restart and independent processes.
  The SDK host binding is preserved, not bypassed. Constructor connectivity errors retry.
- [ ] Verify the reported dev environment's host-identity stability when its VS Code
  node is available; the recovery fix does not claim that an unstable host is repaired.

## Minimal config-entry layout

- [x] Version 5 normally retains only `type` and `url` in data. Existing HA unique IDs
  carry login names; legacy entries without one retain only the needed `username`.
  No sessions, SDK snapshots, internal user IDs or repeated account payloads remain.
- [x] One private atomic Store owns the full SDK snapshot, retaining required host
  binding, hardware/push/replay metadata and opaque future SDK fields. This is a
  storage-layout improvement, not a claim of encryption at rest.
- [x] Migration fsyncs before stripping credentials; v4 does not rerun camera migration.
  Latest matching private state wins over embedded legacy snapshots. Failed writes or
  metadata updates retain retryability without rolling back replay counters.
- [x] Initial creation privately stages credentials until HA supplies the entry ID;
  promotion/removal/reauth clean up staging. Failed cleanup keeps its retry reference.
- [x] Shared locking and private login revisions serialize saves/replacements and reject
  superseded clients. Cancellation cannot abandon an acknowledged replacement or reload.
- [x] Real HA stop/restart, storage failure/retry, old-writer races, missing/foreign
  credentials, legacy login-name recovery and all direct/MFA storage-error forms are tested.

The dev report was an HA-only restart, not a container recreation. Diagnosing its actual
host-identity instability remains blocked on the disconnected/pending-approval VS Code node;
neither the recovery fix nor the compact layout bypasses the SDK's host binding.
