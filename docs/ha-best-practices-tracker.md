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


## VS Code review follow-up (2026-11-12)

- [x] Run synchronous SDK push diagnostics in HA's executor, avoiding a lock-held
  token-save/event-loop cycle. Regression uses the real SDK worker lock and
  actual private Store callback/fsync, with a bounded persistence deadline.
- [x] Detect sticky SDK fatal workers in startup and health checks. Authentication
  requests reauth once; persistence and unknown fatal errors pause retries and
  log distinct corrective/reload guidance. Polling remains independent. SDK 2.1.0
  uses two untyped HTTP-auth markers; match those exactly, plus typed auth causes
  (including long-link AuthenticationRejected). Never treat persistence as auth.
- [x] Exercise real SDK get_mqtt_client caching and PushWorker fatal retention,
  real SDK HTTP registration/refresh and long-link auth errors, storage errors,
  unknown fatal errors, repeated detection, redaction and shutdown.
- [x] Prominent README Breaking change section and PR title/body marking for the
  write-only encryption-key text state, including affected unique-ID suffix and
  automation migration action. Keep security behavior and stable entity/action IDs.
- [x] Repairs fixtures call HA's own domain setup/manager/platform loader instead
  of constructing its private data layout. Only HTTP registration and the existing
  setup/reload boundaries are synthetic. Both supported published runtimes pass;
  the disconnected VS Code container itself has not been executed here.

### Lifecycle follow-up after merged PR236

- [x] Siren: retain restored/new timer cancellation handles, cancel on retrigger
  and entity removal; prove an old timer cannot end a newer activation.
- [x] Camera: refresh selected RTSP credentials and existing stream source after
  a committed key/options change; cover active-stream and unload ordering.
- [x] MQTT device lookup: replace deprecated global lookup with an entry-scoped
  API before HA 2027.8, preserving HA 2026.10.4 support and event identity.

The follow-up cancels restored and new siren timers, fences already queued callbacks
by activation generation, and cancels on entity removal. RTSP sources read committed
camera options, refresh existing streams on selected credential/path/address changes,
and serialize creation with removal. Removal stops preloaded workers without changing
the saved/shared preload preference. Unselected encryption keys do not restart streams
using verification credentials. RTSP userinfo is percent-encoded.

Push now resolves `(DOMAIN, serial)` using the owning entry ID with
`async_get_device_by_identifier`, available on both supported releases. Unknown or
other-account devices retain `device_id: null`; event names and identifiers are unchanged.

Eight offline regressions fail against merged PR236 and pass with these fixes.
Native suites: **395 tests each** on HA 2026.10.4, 2026.11.0 and the local Core dev
checkout; config-flow/diagnostics coverage remains 100% on both published versions.
Core lint/format/Pylint/mypy, translations and scoped hassfest pass; the documented
custom SDK dependency exception remains. Acceptance: 52 tests; standalone: 44 tests.
No live cloud/device calls, deployment, or entity/action identity changes.

### PR structure

The recommendation to separate compatibility from unrelated features is valid.
This patch adds only review-blocker recovery, regression coverage and documentation;
it does not add those three pre-existing lifecycle fixes or rewrite published PR
history. A future split should keep the SDK bump, channel-99 migration and durable
callback ownership together; separate device-family features and independent
credential/media hardening. PR236 has **not** been split; no repository-local
split mandate was found in this checkout. The supplied review may refer to the
VS Code core checkout's instructions, which were not attached.
