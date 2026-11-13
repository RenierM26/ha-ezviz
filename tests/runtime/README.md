# Native Home Assistant tests

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

CI requires **100% statement and branch coverage** of `config_flow.py` and `diagnostics.py` on both HA
versions. Defensive exception handlers are included, not marked `no cover`.

```sh
python -m pip install -r tests/runtime/requirements.txt 'homeassistant==2026.11.0' 'pyezvizapi==2.0.0'
python -m coverage run --branch --source=custom_components.ezviz_cloud.config_flow,custom_components.ezviz_cloud.diagnostics -m pytest --asyncio-mode=auto -o asyncio_default_fixture_loop_scope=function tests/runtime
python -m coverage report --show-missing --fail-under=100
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
with synthetic NIC identities. Constructor authentication errors trigger reauth;
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
client must receive no calls. Existing `test_push_recovery.py` keeps the real
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
