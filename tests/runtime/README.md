# Native Home Assistant tests

> This branch requires published SDK 2.6.0. See [multi-lens support](../../docs/multi-lens.md).

These tests run on the minimum supported HA 2026.10.4 and current HA 2026.11.0,
with the published minimum SDK, without overriding HA's dependencies or applying
version-pinned test-plugin patches. `conftest.py` provides isolated real HA core,
config entries, registries and storage, and prohibits network connections and DNS.

## Config flow

`test_config_flow.py` replaces the old core-checkout-only
`tests/components/ezviz_cloud/test_config_flow.py` suite (which mocked the obsolete
`login()` boundary). It uses real config/options flow managers and schema validation,
patching only SDK/RTSP IO and integration setup/reload. SDK tokens and camera values
are synthetic fixtures; no real credentials or device requests are needed.

Coverage includes setup with EU/RU/custom hosts, duplicate accounts, all login and
MFA failures/retries, context-bound reauthentication, legacy token recovery,
account-change rejection, cloud options, camera selection, category-based defaults,
manual/fetched credentials, encryption/sticker MFA, partial-result prefills, RTSP
verification/encryption auth, failure recovery, and non-persistence of ephemeral
codes/flags. One explicitly documented direct handler test checks the defensive
no-pending-MFA guard, which cannot be reached through a normal menu.

Setup and cloud-options timeout forms also pass through HA's actual HTTP-view
serialization and JSON response generation. Flow-manager validation alone does not
prove that the frontend can render a schema: an arbitrary callable such as
`_validate_timeout` validates inputs but fails field serialization with HTTP 500.
Keep form schemas serializable (integer plus range); perform additional strict
checks in the step before SDK calls or option writes. Error forms are serialized
too, and booleans must not become integer timeouts.

CI requires **100% statement and branch coverage** of `config_flow.py`,
`options_flow.py`, `camera_flow.py`, `flow_validation.py` and `diagnostics.py` on
both HA versions. Extracted helpers remain inside the same gate. Defensive exception handlers are included, not marked `no cover`.

```sh
python -m pip install -r tests/runtime/requirements.txt 'homeassistant==2026.11.0' 'pyezvizapi==2.6.0'
python tools/check.py native
```

Guidance checked against current Home Assistant developer documentation:

- https://developers.home-assistant.io/docs/development_testing/
- https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#reauthentication
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow-test-coverage/

Migration fixture provenance is documented in `fixtures/migration/README.md`.

## Best-practices regression controls

`test_best_practices.py` adds real entity-platform/state-machine and entity-registry
rename tests, write-only credential state checks, and real Repairs manager/retry tests.
It uses the published SDK boundary and the same network-denied native HA environments.
`test_config_flow.py` also checks strict hosts/timeouts and unloaded/stopping/removed
camera options. See [the six-item tracker](../../docs/ha-best-practices-tracker.md).

`test_reliability.py` checks cancellation-safe key changes/fetches, per-camera ordering,
concurrent options conflicts/merges, retryable cleanup outcomes, actual HA stop/restart
persistence and entry removal during an in-flight change. `test_media_source.py` exercises
malformed/stale identifiers, validated limits/URLs, legacy resolution and vendor errors.

`test_token_identity.py` exercises the actual SDK host-feature-code rejection through
HA's entry setup/re-authentication handler, real storage restart, and fresh subprocesses
with synthetic NIC identities. SDK defaults still reject host changes, while `core_policy/test_persisted_identity.py`
qualifies explicit adoption/persistence and fresh HA/SDK process restoration across
different simulated MACs, pre-login fsync, concurrency, cancellation, storage
failures, MFA/reauth reuse and removal. Constructor authentication errors trigger reauth;
connectivity errors retry without discarding or rewriting saved tokens.

`test_token_layout.py` qualifies version-5 minimal config data and the single canonical
private SDK snapshot: legacy import precedence, fsync failures, post-write metadata
interruption, real disk restart, private staging cleanup/retry, reauth cancellation,
stale-writer ordering, foreign-account rejection and legacy login-name preservation.
All initial/reauth direct/MFA storage-error forms are exercised. Exact opaque snapshots
are compared, including synthetic future SDK fields and push replay state.

## Diagnostics

`core_policy/test_diagnostics.py` compares the synthetic account snapshot with
`fixtures/diagnostics.json` and exercises the actual HA download view for account
and selected-device JSON. Only the wrapper's host metadata is synthetic; routing,
registration, ownership, handlers and JSON serialization use real HA. Additional
controls cover missing/stale and registry-only devices, failed polling, pending
cleanup, unloaded entries, legacy caches, unexpected nested/non-finite values,
unknown capabilities, enabled/disabled entities and credential omission. The SDK
client must receive no calls. Existing `core_policy/test_push_recovery.py` keeps the real
SDK-lock/HA token-save deadlock regression and checks fatal recovery categories.

## Core checkout rules

See [Core development checks](../../docs/core-development-checks.md) for the
scoped Core-namespace policy runner and the moving-dev CI job.
`core_policy/test_core_contracts.py` adds behavioral controls for the source-layout/typing
alignment while the published-version matrix protects the supported minimum.

The focused `core_policy/` package also follows Core’s **test-specific** Ruff
policy and enforces complete parameter/return annotations with `ANN`. It is
checked both by normal lint/format CI and the actual moving-Core policy runner.
Older modules remain under the independent legacy test policy.

## Lifecycle follow-up

`core_policy/test_lifecycle.py` covers restored/new siren timers, queued stale
callbacks, retrigger/manual-off/removal, committed RTSP credential selection and
existing source refresh, creation/commit/unload ordering, real HA preloaded stream
restart/stop with preserved preferences, and entry-scoped push device resolution.
Actual registry/entity-platform renames re-add the same camera and siren objects
on supported HA versions. Controls cover both restored-on and no-restored-state
add hooks, fresh real HA stream creation with preload preferences preserved,
coordinator source updates after rename, working siren state/timers, and pending
siren command results fenced across removal/re-add. The stream boundary does not
start a decoder worker. These controls use synthetic SDK IO and never contact
cameras or the cloud.

## Request failure messages

`core_policy/test_errors.py` exercises the published SDK's empty `HTTPError`
chained to a real `requests.HTTPError`, API response-summary codes, transport
failures, actual HA exception translation loading, malformed cause cycles,
secret-bearing payload omission, alarm recovery without reauth and permission
failures across camera action platforms. Network access remains prohibited.

Messages retain HTTP vs API numeric codes without forwarding arbitrary SDK text,
URLs, response bodies, account names or credentials. A 403 indicates a permission
rejection, not proof that a device is shared or unsupported; the owner/app check
is guidance, not an automatic capability change. A 504 is a request timeout, not
a reason to clear credentials, restart HA or initiate reauthentication. Existing
coordinator handling for explicit SDK authentication exceptions is unchanged.

Actions raise translated `HomeAssistantError` once and retain their exception
cause; they do not also log the same failure. Shared polling retains HA's
`UpdateFailed` suppression/recovery behavior. Image/media/push workers keep their
separate failure and lifecycle policies; no verbose payload logging is enabled.


### Active siren rename and SDK fallback qualification

Active siren renames use the real restore lookup (which returns no state at the
new ID on HA 2026.10.4) and a controlled monotonic clock. A rename after 20 seconds retains the
remaining 40 seconds, a second rename retains 30, and re-add after the deadline
reports off immediately. No extra firmware minute is added by renaming.

Published SDK 2.2.0 retains the primary HTTP cause when both switch endpoints
fail ([SDK #436](https://github.com/RenierM26/pyEzvizApi/pull/436)); it is now the
manifest and CI minimum. No `__context__` guessing is needed for mixed statuses.

`core_policy/test_errors.py` now exercises real SDK PUT/POST fallback through HA
light, camera-switch and inventory-plug adapters, in both directions. The 24
cases cover 403/403, 504/504, 403/504 and 504/403, preserving the primary status,
HA/SDK cached states, safe translated errors and no refresh/reauthentication on
failure. Only requests transport is mocked. All 24 fail with SDK 2.1.0 and pass
with published 2.2.0; no editable SDK or live account/device requests are used.

The standalone cross-check remains available from the repository root:

```sh
python -m tools.qualify_switch_errors
```

It checks all four primary/fallback HTTP pairs using real SDK wrapping/fallback
and HA classification. Run with SDK 2.2.0 or newer in a native HA environment.
SDK 2.1.0 remains an intentional negative control, not a supported minimum.


## Ownership audit

`core_policy/test_ownership.py` adds actual HA setup/unload/removal and private
Store filesystem controls for late setup failure, transient unlink retry at all
three private file stages, replacement-account identity preservation and retry
through HA stop. A platform-refused unload remains an unrecoverable HA state with
its working owner retained; the test does not fabricate an unsupported retry.
See the [audit/evidence matrix](../../docs/lifecycle-audit.md) for related existing
controls, closed gaps and process-local cleanup limits.


## Flow extraction

The canonical HA handler/factory remains in `config_flow.py`. Existing flow/HTTP
serialization, MFA, partial credential, options conflict and storage controls now
exercise the extracted options/camera/validation modules. The RTSP transport patch
targets `camera_flow`, not a stale re-export. Coverage includes every extracted
flow module; see [flow modularity](../../docs/flow-modularity.md).


## Behavioral contracts and incremental policy migration

The root collection guard refuses native tests in a plugin-installed environment
and refuses mixed/unscoped collection. The migrated
`core_policy/test_push_recovery.py` retains all 13 prior SDK-lock/fatal controls
and adds four actual SDK worker transient-recovery/shutdown controls. See
[the F05 audit and evidence](../../docs/behavioral-tests.md). Other legacy modules
are not claimed to have been migrated to strict Core test policy.

## Options reload ownership

`core_policy/test_options_reload.py` completes cloud and camera options through
HA's real options manager with an attached camera. Changed values reload once;
unchanged submissions do not reload. `OptionsFlowWithReload` cannot coexist with
any config-entry update listener, including entity-local listeners. Older flow
tests had no attached camera, while camera tests updated options outside the flow
manager; neither exercised this combination.

Form changes use HA's reload. Credential actions continue to commit and notify
the coordinator directly, updating existing streams without reloading the account.
The lifecycle tests retain selected-key, URL escaping, address-change and removed
entity controls for this path. Reload itself is mocked in the options regression;
this is not live browser or camera playback qualification.

## Clear-image and HTTP privacy regressions

`core_policy/test_image_decode_failure.py` uses generated JPEGs containing the
vendor marker in legal COM/APP metadata, both freshly fetched and pre-cached,
for Last motion and Snapshot. A content signature takes precedence over marker
substrings; this is not full image validation or vendor-frame parsing. HA's
four-byte magic table alone misses COM-first JPEGs, so the guard also recognizes
the standard three-byte JPEG signature. Residual cipher and decode-failure
recovery tests remain in place.

HTTP privacy tests patch the HA-owned client's `get`, not `_fetch_url`, so timeout,
request and HTTP-status failures exercise the real error-logging boundary. They
assert that synthetic private paths, signatures and exception text are absent,
and that a later success is fetched/cached normally. The override preserves HA's
client, timeout and redirect policy. The exact `httpx` lint exception retains the
client exception types used by the supported HA releases; it is not an HTTP stack
migration. These are offline tests, not camera or deployment qualification.

## Intercom history baselines

`core_policy/test_intercom_history.py` uses the published SDK's real
`poll_devices()` and native HA event entities. It checks a failed initial history
request followed by successful history during stale camera rendering, recovery
without old last-event publication, empty baselines, shared deduplication,
unsupported rows excluded from the SDK query, and independent live push admission.
Flow/diagnostic coverage percentages do not describe event-module coverage.

Run these probes through the native harness, which denies sockets and DNS before
SDK execution. Synthetic serials alone do not prevent network access: an empty
prefetched-alarm mapping can cause the SDK renderer to perform a cloud fallback.


## Point PTZ action (published SDK 2.5.1)

`core_policy/test_ptz_position.py` exercises native service registration/targeting,
real SDK serialization, off-loop commands, native coordinate/lens schema
validation, capability-qualified routing, unchanged observed state, no stream
startup, API rejection, and cancellation/shutdown ownership. Ordinary directional
PTZ is not proof of point control: direct586 and selected-point724 paths have
distinct requirements.

Published SDK 2.5.1 supplied the corrected point payload. SDK 2.6.0 adds
SDK-owned channel discovery/routing and batch images. CI installs the exact
PyPI release and checks that it satisfies the integration manifest.


The native Lens selector exposes `auto`, `1` and `2`, with translated labels.
Explicit choices match reported video channel indices, not metadata list order;
reordered rows retain their target. Missing or duplicate selected channels fail
without moving another lens. Automatic rejects ambiguous selected-point routes
with an instruction to choose a lens. Direct586 retains only the fixed app route.
Tests cover schema/selector metadata, actual SDK URLs/payloads, unsafe vendor
indices and rejection of obsolete experimental resource/index fields. They do
not establish which physical lens moves, render the frontend or issue live PTZ.


## Per-lens targets

`core_policy/test_camera_channels.py` covers native translated camera/image/event
entities, stable identities and registry restoration, bound PTZ, isolated capture
URLs/signals, explicit stream paths, batch image downloads/caching, stale-key and
shutdown fences, scoped/deduplicated notification delivery and options conflicts.
Media checks exercise channel-matched history, opaque reference binding, SDK
image selection and retained auth/public-address protections. These are offline
candidate checks, not browser, firmware or physical-movement qualification.
