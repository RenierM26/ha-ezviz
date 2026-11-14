# F05 behavioral contracts and test isolation

Baseline: merged PR #242 (`8899da6`), published SDK 2.2.0, HA 2026.10.4 and
2026.11.0. This slice changes no distributable integration sources.

## Collection is a contract, not just documentation

The root `conftest.py` classifies explicit filesystem targets before pytest
imports test modules or runs fixtures. Each invocation must select exactly one
layer:

| Layer | Supported target examples | Environment |
| --- | --- | --- |
| Standalone | `tests/test_device_contract.py`, multiple root `tests/test_*.py` files | Tooling/SDK environment; fake HA boundaries are intentional here. |
| Native | `tests/runtime`, a focused directory/file/node ID below it | Real HA; **no** `pytest-homeassistant-custom-component` package, even with autoload disabled. |
| Acceptance | `tests/acceptance`, a focused file/node ID below it | Separate matching-plugin HA environment. |

Use `tools/check.py standalone`, `native`, `acceptance`, or
`core --core /path/to/core --runtime-tests` for complete profiles. Do not combine
layers with `-k`/deselection or use root-level unscoped `pytest`, `pytest tests`,
package-based `--pyargs`, or unclassified paths. The guard returns pytest's usage
error (exit 4) with the supported commands. Absolute paths and node IDs work,
including invocation from another directory. Editor discovery remains native-only.

This protects accidental collection, not deliberate pytest configuration bypasses.
Pytest/plugin configuration imports may happen before the session-start hook;
the guard does not undo an already imported plugin. The native profile refuses
the plugin-installed environment rather than claiming it can remove its patches.

`tests/test_test_layer_isolation.py` uses the actual guard in private temporary
trees and actual pytest subprocesses, not mocked collector methods. Fourteen
controls qualify safe collection, mixed/default/unscoped rejection before a
sentinel module import, paths/node IDs, unsupported targets and plugin discovery.
An unguarded control really imports the sentinel, distinguishing policy rejection
from ordinary import-time failure. A separate qualification command also rejects
native collection in the actual plugin-installed HA acceptance environment.

The guard itself is included in shared formatting/type checks, and both CI and
moving-Core workflow filters watch it. No isolation override/waiver is added.

## Push worker ownership and outage/recovery

`tests/runtime/core_policy/test_push_recovery.py` replaces the legacy module at
`tests/runtime/test_push_recovery.py`. Its original 13 cases now use complete
annotations, docstrings, formatting and the actual Core test-policy overlay:

- SDK diagnostic-lock contention must not block HA's loop while a real durable
  Store callback needs that loop to finish.
- SDK cached fatal workers stay fatal across stop/reconnect. Registration,
  refresh and handshake authentication failures request one reauth; persistence
  failures (even with an authentication cause) and unknown fatal failures do not.
  Polling remains independent and private error details stay out of logs.

An AST comparison against immutable `8899da6` strips annotations/docstrings only
and confirms the original test bodies, decorators and assertions are preserved.
No original cases or policy checks are dropped during the move.

Four additional native cases use the real published SDK client, MQTT wrapper,
`PushWorker` thread/retry loop, HA handler/health monitor, polling coordinator and
runtime cleanup owner:

| Boundary | Observation |
| --- | --- |
| Session raises `OSError` or `TimeoutError` | SDK creates a fresh session; transient failure count increments. HA does not restart the SDK client or request reauth. |
| Session returns unexpectedly | SDK reconnects with another session without misclassifying a normal disconnect as an exception. |
| Recovery session gated at IO | Actual SDK account polling remains healthy and the accepted camera stays available. Releasing the gate restores synthetic session readiness. |
| Shutdown while SDK waits 60 seconds before retry | Real runtime shutdown interrupts the wait within a two-second test deadline; the worker is joined before HTTP close, and no second session is created. |

Account acquisition and session IO are synthetic. SDK validation, snapshot
rendering, thread/retry lifecycle and HA ownership remain real. Session readiness
is not proof of a live broker or physical push delivery. Tests use events and
bounded waits, not a 60-second sleep, and release cleanup barriers on failure.

These four cases qualify preserved behavior; they are **not** claims of newly
fixed production regressions. Existing registry/restoration, SDK HTTP fallback,
credential ownership and real mixed-inventory retention controls remain in their
separate modules. This slice does not fabricate additional bugs for a test count.

## Qualification and remaining scope

- Native HA 2026.10.4, HA 2026.11.0 and Core dev: **481 passed each**.
- Plugin-backed HA 2026.10.4 acceptance: **52 passed**.
- Isolated standalone: **60 passed**; focused migrated/expanded push module:
  **17 passed** (13 retained, four new).
- Core production/test Ruff and formatting, strict Pylint, mypy, generated
  translations and scoped hassfest pass. Repository mypy checks 48 source files.
- Existing five-module flow/diagnostics coverage remains 100%: 513 statements,
  86 branch outcomes. That scoped gate is unchanged, not whole-repository coverage.
- Native socket/DNS IO is prohibited. The existing custom-only SDK dependency
  exception remains explicit; built-in/custom coexistence is not qualified.

Legacy policy migration remains incremental. The other existing native modules,
standalone fake-HA controls and plugin-specific acceptance tests keep their stated
scope and policy; this PR does not claim to modernize every historical test. Move
future cohesive slices under Core test policy while preserving their assertions
and real producer/registry/storage contracts. Do not replace them with source-text
checks or mirror implementation details just to increase coverage.

No live account/camera commands, HA restart, installation/deployment, SDK/version
change or release occurs here. Live cloud/push outage duration and physical device
behavior remain outside offline qualification. F06 user docs/release discipline
is the next overhaul phase.
