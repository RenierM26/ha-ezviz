# Home Assistant Core development alignment

The integration stays compatible with HA **2026.10.4+ / Python 3.14.2+**, while
checking the current Core development policy. First qualified against Core
`462fe2cc3549abf05b91d00e205447b9521a5c75` (`2026.12.0.dev0`), Python 3.14.10,
and the published SDK minimum `pyezvizapi==2.3.0`.

## Repeatable checks

`tools/check_core_policy.py` accepts a Core checkout and an interpreter containing
that checkout's runtime, development requirements, hassfest package, this repo's
native test requirements and the published SDK minimum. For example, in a new
isolated venv (not the running HA environment):

```sh
python3 -m pip install -e /path/to/core -e /path/to/core/packages/hassfest \
  -r /path/to/core/requirements_test.txt -r tests/runtime/requirements.txt \
  'pyezvizapi==2.3.0'
python3 -m pip check
python3 tools/check_core_policy.py --core /path/to/core --runtime-tests
```

This is a **scoped integration check**, not Core's full `script/setup`, full
integration dependency installation or whole-repository test suite. In particular,
Core's `requirements_all.txt` pins the incompatible built-in EZVIZ SDK. Do not
install that set into the custom integration's qualification environment or
silently replace its SDK with an unpublished checkout.

The checker logs the Core commit, Python/tool/runtime versions and integration
source hashes. It creates an ephemeral package overlay with unchanged integration
Python sources under `homeassistant.components.ezviz_cloud`. Built-in Core modules
are linked for module lookup; the original Core sources and the user's custom
integration sources are not edited. Generated English output is confined to the
temporary copy. Checks use **Core's actual configuration**, not a relaxed custom
namespace configuration:

- Generate English translations with Core's translation tool and compare content.
- Ruff lint and formatter.
- Pylint including HA-specific checks, one worker, scoped to this integration.
  Common-argument checks are strict (`--ignore-missing-annotations=n`), including
  `hass: HomeAssistant`. An ephemeral `hass: Any` negative control must emit
  exactly E7402 before production lint runs; it is removed before mypy/tests.
  The former `y` override also skipped annotated common arguments, not merely
  missing annotations, and therefore masked eight token/worker annotations.
- Mypy in the Core integration namespace, with Core's plugin and import policy.
- Hassfest on the **custom** integration path, using the existing exact-conflict
  wrapper. Every other validator failure remains fatal.
- Optionally, the integration's native real-HA tests, with plugin autoload disabled.

The custom path matters: checking it directly can skip HA-specific Pylint checks;
mypy applies different defaults outside `homeassistant.components`. Core's built-in
`ezviz` tests do not test `ezviz_cloud`. The native suite uses synthetic accounts
and blocks network connections/DNS; it performs no live acquisitions or commands.

`.github/workflows/core-policy.yaml` runs this against moving Core **dev** on PRs,
weekly, or manually against a specified Core ref. The published-HA native matrix
in `ci.yaml` remains separate, with 100% coverage across the canonical/extracted flow modules and diagnostics.

## Local lint and formatting

`tools/core_ruff.toml` is a reviewable snapshot of Core's Ruff policy at the
qualified commit above, with the Python target that Core otherwise infers from
its project metadata. CI explicitly selects this policy for component sources:

```sh
ruff check --config tools/core_ruff.toml custom_components/ezviz_cloud
ruff format --check --config tools/core_ruff.toml custom_components/ezviz_cloud
```

The root Ruff configuration now extends this Core snapshot, so plain `ruff check .`
also checks component sources. Legacy standalone tests/tools have scoped configurations,
and focused Core tests retain their own overlay. The explicit commands above remain
useful for production-only checks. CI runs both scopes. There is deliberately no Ruff
configuration inside the distributable integration: a copied component must not
override its host Core checkout's policy or refer to absent repository tooling.
The live Core job catches later policy drift; refresh the snapshot and provenance
when adopting a new baseline, rather than weakening Core's configuration.

## Deliberate compatibility and ownership exceptions

- `async_setup_entry`, `async_unload_entry` and `async_migrate_entry` still return
  booleans: the supported HA minimum explicitly requires them. Three line-scoped
  Pylint exceptions document the newer policy mismatch. Do not change these hooks
  to `None` without changing and requalifying the supported minimum.
- `_runtimes` and `_token_stores` retain cleanup, shared locks and token revisions
  after HA clears `entry.runtime_data`. The four line-scoped runtime-data exceptions
  preserve those ownership anchors; platforms still use `entry.runtime_data`.
- Broad exception boundaries that retain worker cleanup/retry ownership, or preserve
  other cached diagnostics when a getter fails, have local explained Ruff exceptions.
  They are not blanket disabled and are covered by failure/cancellation tests.
- Stock hassfest's exact built-in/custom SDK conflict remains unresolved:
  `pyezvizapi>=2.3.0` versus built-in `pyezvizapi==1.0.5.0`. The wrapper visibly
  demotes only that conflict. This is not a claim of unmodified hassfest success,
  built-in/custom coexistence, upstream Core acceptance, or production deployment.

## Regression contracts

`test_core_contracts.py` checks active light color mode, one flat image-addition
batch with preserved lens/snapshot unique IDs, early action registration and actual
HA dispatch across two account platforms (including optional capture responses),
resolved English selector translations and initial Store acknowledgement state.
Existing native tests exercise the probatio schemas, serialization/errors/MFA/repairs,
entry boolean hooks, cancellation, retained owners, durable writes, migration and
registry identity. The source refactoring does not change entity unique IDs, action
names, credentials, camera configuration or the SDK minimum.

## Test-policy scope

Core has a separate `tests/ruff.toml` overlay. In particular, it permits private
member access in tests and allows/import-sorts test helpers differently from the
production policy. Applying only Core's root config to tests is not equivalent to
running Core's own test rules. We keep protected-state fault injection and the
private durability invariant checks; no production accessors were added merely to
satisfy test lint.

The two new contract/diagnostics modules now live in `tests/runtime/core_policy/`,
an explicit package with Core-test imports, docstrings and concrete fixture/test
annotations. The rest of the native suite uses explicit `tests.runtime` helper
imports so package collection does not duplicate modules. Its older modules have
not all been converted to Core test style.

The focused package inherits `tools/core_tests_ruff.toml` (Core's test overlay at
the same recorded baseline) and additionally enables Ruff `ANN` annotation checks.
Normal lint and CI formatting cover the whole package, including future additions.
Local/editor/CI commands are shared through `tools/check.py`; see
[development setup](development.md) for the isolated environment profiles.
The moving-Core checker copies it unchanged, loads **that checkout's** actual test
policy (from Git when sparse checkout omits the file), logs its Python hashes, and
runs test lint/format plus annotation checks separately from integration checks.
The only policy-path adjustment in the local snapshot is its `extend` target.

Freshness edge cases now exercise the exported diagnostic report rather than
calling private serializer helpers directly. Registration, retained worker/setup
states and Store acknowledgement still use internals where they are the subject
of the test. The fake HTTP request uses HA's existing string authentication key;
the resulting aiohttp warning is not an integration runtime regression.
