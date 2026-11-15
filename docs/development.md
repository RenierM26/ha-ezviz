# Development and checks

Python **3.14.2+** is required. These profiles target Linux/devcontainers (or WSL),
matching CI; native Windows environment paths are not provided. Bootstrap creates two repository-local environments
from checked-in profiles; it does not launch HA, authenticate, touch production
configuration, or install every built-in integration requirement.

```sh
python3.14 tools/bootstrap.py
.venv-tools/bin/python tools/check.py lint
.venv-tools/bin/python tools/check.py standalone
.venv/bin/python tools/check.py native
```

| Environment | Profile | Purpose |
| --- | --- | --- |
| `.venv-tools` | `requirements/lint.txt` | Pinned Ruff/mypy/pytest/stubs and published SDK; no HA installation or HA test plugin. |
| `.venv` | `requirements/dev.txt` | HA 2026.10.4, published SDK, native requirements and debugpy; no custom-component pytest plugin. |
| Separate acceptance venv | `tests/acceptance/requirements.txt` plus `requirements/sdk.txt` | HA plugin and its matching runtime. Never install into `.venv`. |
| Separate Core venv | [Core setup](core-development-checks.md) plus `requirements/sdk.txt` | Core's own requirements/tool pins and actual lint plugins. |

`requirements/sdk.txt` is the exact published SDK version shared by CI and the
local profiles; the distributable manifest keeps its minimum requirement. Every
check profile rejects a mismatch between that minimum, the qualification pin and
the installed SDK, rather than silently testing a newer dependency. Fast
tooling pins are shared by local/editor setup and fast CI. This is **not** a full
transitive dependency lock: HA/Core retain ownership of their dependency sets.
When upgrading versions, update and qualify their declared profiles deliberately.

## Editor and devcontainer

The devcontainer uses the same bootstrap. VS Code selects `.venv` for Python/
native discovery and `.venv-tools/bin/ruff` for Ruff. The tracked env file disables
pytest plugin autoload; discovery explicitly enables pytest-asyncio and selects
only `tests/runtime`. Tasks invoke the same `tools/check.py` commands as CI.

Default Ruff discovery now applies the Core snapshot to production. `tests/` and
`tools/` retain their scoped legacy policy; `tests/runtime/core_policy/` retains
Core's test overlay plus annotations. No repository policy file is shipped inside
`custom_components/ezviz_cloud`. `lint` checks that a stdin component probe really
triggers Core-only D100 as well as TID251, preventing the former silent component exclusion from returning.
The separate Core checker also proves its actual Pylint invocation catches E7402.

The manual debug launch uses the `debugpy` adapter and `homeassistant` module;
it does not assume `hass --debug` opens a debugger port. Launching HA is an explicit
human action, not part of bootstrap/tests. The example config logs this component,
not a placeholder domain. Local HA config/state is ignored by Git; never commit
credentials, `.storage`, recordings, tokens, diagnostics or real device fixtures.

The bootstrap and commands are verified in clean repository-local environments.
A green CLI check is not proof that an attached VS Code UI or Docker devcontainer
was rebuilt or exercised; report those separately when testing them.

## Scope and release limits

See [architecture](architecture.md), [test responsibilities](../tests/README.md),
[Core policy](core-development-checks.md) and [overhaul backlog](overhaul-backlog.md).
Do not install Core's complete optional integration requirement set: its built-in
EZVIZ SDK pin conflicts with this custom integration. The explicit hassfest
exception remains narrow and visible. No check command publishes or deploys.


## Release maintenance

See [the release checklist](releasing.md) for version/tag/source/SDK evidence and
[the validation-pin audit](validation-pins.md) for immutable action/image inputs
and intentionally moving Core policy. `tools/check_release.py` is a read-only
source/metadata preflight, not a publisher or substitute for runtime tests.

### Observed-only cloud settings

Ordinary setting writes share `setting_writes.SettingWrites`, owned by the account
coordinator. Camera switches/lights, numbers, selects, plug/bulb power, camera name
and motion detection use `EzvizEntity.async_write_setting`. Buttons, captures,
firmware, sirens, alarm panels and encryption-key text keep their action/security
semantics. All displayed values and availability come from observations. There
are no optimistic display overlays, confirmation readers, generation fences,
confirmation retries or expiry notifications. Getters never wait.

Accepted SDK writes (explicit `False` rejects; void setters are supported) restart
one account-wide refresh timer, about two seconds after the latest ACK. The timer
waits for any related write to finish before requesting a refresh through Core's
existing coordinator/debouncer. Two seconds is a request delay, not a total-latency
or propagation guarantee. An accepted brightness write still schedules a refresh
if the subsequent power write fails. A write during an active refresh retains its
own delayed request. There is no custom retry loop: subsequent ordinary polls
provide eventual observations if the first refresh remains stale or fails.
Controls can therefore retain their previous displayed value until the cloud
reports the change. This is intentional, including during unrelated MQTT pushes.

Writes are serialized and owned across service-caller cancellation. Number/select
SDK adapters receive detached data prepared inside the write lock. For compound
settings only, a command-only cache retains accepted sibling-field projections
for 60 seconds. Day/night, night-vision and speaker/microphone commands compose
these fields with fresh observations so rapid related writes do not clobber each
other. SDK payload helpers preserve normalization and unknown fields; raw user
inputs still pass through SDK validation before projections are recorded. Cached
audio fields cannot recreate missing/invalid inputs required by SDK validation.
The cache is never displayed and makes no confirmation claim. Its TTL is checked
lazily on command preparation, without a timer. Within that brief window a related
command preserves the last accepted sibling even if another client changes it;
after expiry it uses observed data. This bounded command-construction policy is
separate from displayed state. No camera names or encryption keys enter the cache.

On unload the refresh callback/task is cancelled and the cache cleared; owned
command and poll workers finish before HTTP session close. No persisted state,
entity IDs, options, migration or dependency minimum changes are introduced.

The two-second allowance originates from one status LED's read-after-write timing;
no new live-device timing validation is claimed. `test_setting_writes.py` exercises
eight control families, stale/matching/external observations, coalescing, failure,
account isolation, caller cancellation, slow compound writes, writes during an
active refresh and unload with a real HA coordinator/debouncer and synthetic
network snapshots. `test_setting_commands.py` covers compound sibling preservation,
encoded SDK layouts, normalized payloads, queued-before-ACK preparation, original
SDK validation, lazy context expiry and complete/partial audio capability loss.

### Media and earlier audit coverage

`tests/runtime/test_integration_audit.py` exercises the media proxy's server-owned
reference boundary, private literal/DNS targets (including noncanonical numeric
IPv4), safe CDN/relative redirects and unsafe/over-limit/deadline rejection,
published-SDK keyless/encrypted multi-image decoding,
decoded MIME types, registered siren failure/recovery, and camera control writes
across unrelated alarm pushes. All transport is synthetic; these are not live
camera or vendor-CDN validation claims. Existing registry rename/timer/unload
controls continue to run.

`tests/runtime/test_audit_followup.py` verifies Unicode-dot/fullwidth private
hosts at the actual aiohttp connector boundary, intercepting before socket
creation. Initial records and redirects are rejected after yarl normalization;
valid public-host requests retain their original signed query string. Compound
light writes use the real Core refresh/debouncer, with a blocked cloud poll:
brightness and power precede the single refresh, accepted brightness is observed
after power denial/error, and rejected first writes neither power on nor poll.
The older partial-push control test also observes synthetic command side effects
instead of fabricating the requested final state during refresh.

Cold image concurrency tests cover viewer cancellation, URL/key/capture generation
changes, shutdown and concurrent lens downloads. Each entity coalesces download
and decoding; lenses share in-flight downloads but SDK 2.4.0 still decodes each
lens independently. A future SDK batch-selection API can optimize that remaining
CPU duplication without moving channel/framing rules into this integration.

The media proxy starts only from public HTTPS sources registered while browsing
SDK-backed camera/alarm records, never a URL supplied by the HTTP caller. Its
resolver rejects private targets at every connection. It manually follows at
most five redirects (301/302/303/307/308), resolving relative locations against
the actual response URL and rechecking HTTPS, public IPs, credentials and ports
on every hop. Intermediate responses are released; a single 30-second deadline
covers the chain and final body. Missing/malformed locations, downgrades, private
DNS/IP targets and excessive chains fail closed. Only minimal headers are sent.
Old proxy references expire on unload/restart or bounded-registry eviction;
browse again to obtain a current reference. No new SDK/HA minimum or migration.
Host checks use `yarl.URL(raw_url).raw_host`, matching aiohttp's literal-IP path
which bypasses DNS resolvers; the original URL string is still sent unchanged.


### Shared local-device controls

The camera-settings `local_control_auth` selector is disabled, verification code,
or encryption key. Only that source enum is saved; the existing username/key
fields and SDK-observed address supply the connection profile. This does not
change `rtsp_uses_verification_code`, which still selects playback authentication.
The optional RTSP wake/DESCRIBE test defaults off. Its playback-credential
selector renders native string choices through a small boolean adapter so old
`rtsp_uses_verification_code` storage/API submissions remain compatible.

| Module | Responsibility |
| --- | --- |
| `local.py` | Shared source enum, profile validation, current-credential/address resolution and per-camera owned execution/lock. Reused by current and future local features. |
| `local_rtsp.py` | RTSP-specific known-state read, idempotent read/modify/bounded read-back using public SDK methods. No credential storage or flow logic. |
| `coordinator.py` | Entry-owned shared `LocalDevice` instances and `LocalRtspCoordinator`'s five-minute observed state/polling lifecycle. |
| `camera_flow.py` | Existing cloud/MFA credential resolution, followed by read-only local validation with partial-result errors. |
| `options_flow.py` | Camera settings dropdown, preserved selection through errors/MFA, conflict-aware allowlisted save and old unpublished-form cleanup. |

Shared execution resolves current options inside the lock, rather than retaining
a camera password in a coordinator. Reads and writes retain ownership until the
SDK worker finishes even when a waiter is cancelled. The RTSP coordinator keeps
read-back publication owned too. The account caches one RTSP coordinator per
camera, shared by the settings flow and the entity. Both enable paths publish
verified read-back through this same owner, even if options remain unchanged or
the later DESCRIBE test fails. Candidate credentials remain operation-local; this
is state ownership, not a persistent TCP session. Account close shuts down cached
coordinators, including flow-only instances, and rejects new ones. Late result
publication is skipped once account shutdown begins, so it cannot recreate a
canceled polling timer. Core listeners and entry unload stop polling.
Platform setup publishes cloud and local entities without waiting on LAN IO.
Local coordinators start unavailable; entry-owned background tasks perform initial
reads, bounded to two concurrent cameras. Unload cancels queued initializers while
already-dispatched SDK work remains account-owned and drained before HTTP closes.
Future local controls can use `account.local_device(serial).async_execute(...)`
without sharing RTSP state or adding another credential profile. Flow validation
uses that same executor with explicitly resolved candidate options; it never
commits credentials before successful validation.

`test_local.py` covers the shared profile and real camera-options/MFA flows:
explicit source versus independent media selection, no fallback, queued current
key/address resolution after a cancelled waiter, fetched keys, opt-out, partial
errors and concurrent edits. `test_local_rtsp.py` retains SDK response/payload,
idempotence, off-loop IO, read-back, cancellation and unknown-outcome controls.
Core-policy RTSP tests retain actual registered services, translated names,
device association, rename, optional setup isolation, polling teardown and
allowlisted diagnostic privacy. All transport remains synthetic/network-denied.


#### Guided RTSP setup

Credential resolution can defer its RTSP probe. Options always validate enabled
LAN control before that optional test, and a failed validation retains the source,
resolved fields and requested-test flag through both normal and MFA paths. Only
known off plus requested RTSP testing sets an ephemeral enable offer. The one-shot
`rtsp_enable` form defaults false and saves nothing. Unchecked submission still
runs the requested probe without writing, reports its auth/connection errors with
fields retained, and saves only on success. An affirmative submission
uses the shared executor's in-lock precondition to reject stale camera settings,
a replaced runtime or explicit SDK `health.present=False` even when the SDK retains
the historical camera row. It repeats ownership/presence checks before probing and
committing. Stale `health.current=False` and temporary cloud failures alone do not
revoke independent LAN access,
then performs one idempotent write followed by read-only recovery checks within
a 75-second monotonic window, with waits capped at five seconds. An already-started
SDK read can overrun that window; no later read starts after expiry. Live Husky
disable acknowledged successfully but management only recovered after about a
minute. All waits remain off-loop under the owned per-camera lock. The switch
platform uses `PARALLEL_UPDATES=0`: cloud writes already use the shared account
write lock, while local writes use their device lock, so service recovery cannot
hold unrelated switches behind a platform-wide semaphore. Matching state
is authoritative even if the write ACK was interrupted; writes are never retried.
The requested probe follows;
only success saves options. The result distinguishes configured-on from DESCRIBE
success; failures after a verified enable explicitly retain that fact. No rollback
or write retry is claimed. New flow fields/one-time codes never enter the saved
allowlisted patch. `test_rtsp_setup.py` covers normal/MFA ordering, retained fields,
HTTP selector/form serialization, decline, replay/no-offer guards, conflict before
and during queued IO, post-write conflict, unverified outcomes, and disabled-LAN
or unknown-state cases that must not offer a write. Virtual-clock regressions
cover the observed long Husky recovery and the bounded no-write-retry deadline;
real HA service dispatch verifies cloud switches remain responsive during recovery.


`test_rtsp_offer_lifetime.py` exercises the actual SDK `poll_devices()` empty-record
retention path, queued consent, owner replacement, pre-probe/pre-commit removal and
cloud/stale-health controls. `test_local_startup.py` blocks real executor workers
to prove immediate cloud publication, unavailable local initial state, bounded
concurrent initial reads, eventual success/failure and unload drain ownership.
