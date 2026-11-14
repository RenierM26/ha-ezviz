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
