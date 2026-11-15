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

### Cloud setting confirmation

Ordinary setting writes share `confirmation.SettingConfirmation`, owned by the
account coordinator. Camera switches/lights, numbers, selects, plug/bulb power,
camera name and camera motion detection all use `EzvizEntity.async_write_setting`.
The camera motion-detection control and its switch share the same pending key.
Buttons, captures, firmware operations, sirens, alarm panels and encryption-key
text keep their existing action/security semantics; reads and getters never wait.

After an accepted SDK write (`False` means rejection; void-returning setters are
supported), the manager records an entry-local pending value keyed by serial
and setting, not an entity ID. Snapshots and coordinator data remain observations.
Serialized, owned writes preserve order even if the service caller is cancelled.
Number/select SDK adapters receive detached input so they cannot mutate accepted
camera data. Pending display values survive unrelated pushes and stale reads;
missing number capabilities still render unknown. Availability is never overlaid.

One account-wide refresh request is scheduled two seconds after the latest
accepted write, not immediately after every entity setter. Compound brightness/
power writes restart this delay after the final ACK; accepted brightness remains
pending if power fails. Confirmation uses Core's shared refresh/debouncer (including
its existing cooldown), so two seconds is a request delay, not a promised total
latency. While pending, retry requests are spaced ten seconds apart; normal polls
also confirm. Authentication failures do not start a confirmation retry loop.

Each actual SDK poll captures the current accepted-command generation when its
worker starts. A reused timed-out worker retains that original generation.
Only current, online device observations from a sufficiently new poll can clear
matching pending values. Full poll acceptance is serialized so an older poll
cannot overwrite a newer accepted result. Pushes/manual data notifications cannot
confirm commands. A later accepted setting supersedes its predecessor.

Each pending setting expires 60 seconds after its ACK, publishing the latest
observed value without waiting for another poll. New commands for other settings
do not extend that deadline. Confirmation/expiry callbacks and the confirmation
task are cancelled on unload; owned command/poll workers finish before HTTP close.
Nothing is persisted, including pending camera names; no key text enters this map.

These initial timing constants reflect read-after-write delay measured on one
camera's status LED in both directions. They are not a universal propagation
bound. No new live device timing claim is made by this implementation.
`tests/runtime/test_setting_confirmation.py` exercises all supported control
families with real HA coordinator/debouncer behavior and detached synthetic SDK
network snapshots, including stale-to-stale-to-matching reads, failure, ordering,
pre-command/timed-out polls, account isolation, expiry, cancellation and unload.

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
