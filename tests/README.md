# Test responsibilities

Use [the shared check entry points](../docs/development.md). Do not run unscoped
`pytest` across all layers in one process: standalone fixtures replace HA modules,
acceptance installs a version-pinned HA plugin, and native tests deliberately do
neither. The root collection guard now refuses mixed/unscoped targets with a
usage error before test modules are imported. See [behavioral contracts and
isolation](../docs/behavioral-tests.md) for focused path/node-ID commands and limits. An editor's default test target is `tests/runtime`, not this whole tree.

| Layer | Environment / command | Proves | Does not prove |
| --- | --- | --- | --- |
| Standalone `tests/test_*.py` | `.venv-tools/bin/python tools/check.py standalone` | Small SDK/integration contracts and fake Store/worker failure controls without HA installed. | Real HA platform, storage, dispatcher or shutdown behavior. |
| Native `tests/runtime` | `.venv/bin/python tools/check.py native` | Real HA core, registry, forms/HTTP resources, storage and ownership. Explicit pytest-asyncio only; SDK/device transport synthetic and network/DNS prohibited. | Live cloud, push delivery, cameras, physical action or installed devcontainer behavior. |
| Acceptance `tests/acceptance` | Separate matching-plugin environment; `python tools/check.py acceptance` | Plugin-backed HA inventory/push/integration acceptance on HA 2026.10.4. | An independent unpatched native runtime or newer HA compatibility. |
| Core policy | Core development environment; `python tools/check.py core --core /path/to/core --runtime-tests` | Actual Core production/test lint, strict Pylint, types, translation generation, scoped hassfest and native runtime on that checkout. | Built-in/custom EZVIZ coexistence or live deployment. |

Published-version native CI qualifies HA 2026.10.4 and 2026.11.0. Core dev remains
a separate moving-policy gate with its own pinned tools. Fast repository checks
use `requirements/tooling.txt`; do not force those versions into Core's environment.

Existing 100% config-flow/diagnostics coverage is a scoped regression gate, not
whole-integration qualification. The [native contract notes](runtime/README.md)
record the actual behaviors exercised. New race/fallback tests should pass through
real SDK/HA producers wherever the producer contract is what matters.
