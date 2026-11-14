# Staged repository overhaul

Baseline: merged ownership PR #240 (`cf0ee87`), 2026-11-14. Work proceeds in reviewable
slices. This backlog records implementation status, not deployment approval.

| ID | Priority / state | Deliverable and exit criteria |
| --- | --- | --- |
| F01 | P0 / merged in PR #239 | Shared check entry points; pinned fast-check requirements and published SDK profile; isolated native/tooling dev environments; default component Ruff discovery; VS Code/CI alignment; architecture and test responsibility docs. Production integration files remain unchanged. |
| F02 | P1 / merged in PR #240 | Audit and regression matrix in [lifecycle-audit.md](lifecycle-audit.md). Close late-setup cleanup, transient private-file deletion and replacement-account identity gaps with real HA/Store controls; preserve existing lifetime anchors. |
| F03 | P1 / implemented in this PR; merge pending | Extract account/options/camera-preparation/validation responsibilities from the 1,007-line monolith; canonical `config_flow.py` is now 400 lines. See [flow modularity](flow-modularity.md). Preserve form serialization, step IDs, MFA, options conflict handling, migrations and storage recovery. |
| F04 | P1 / backlog; after F02 | Inventory raw camera dictionaries vs typed SDK snapshots; define accepted state and availability boundaries, then consolidate adapters without copying SDK vendor logic or inventing permission/capability conclusions. |
| F05 | P1 / backlog; cross-cutting | Expand real producer, registry/restoration and outage/recovery contracts. Migrate legacy tests to Core test style incrementally; retain lightweight fake-HA controls only for their stated scope. No coverage-number-only goal. |
| F06 | P2 / backlog | Replace legacy user onboarding with verified setup/support/troubleshooting/migration docs; define HA version/tag/release and SDK adoption checks, audit floating validation images/actions and make release evidence reproducible. Publication/deployment remain separate actions. |

## Audit observations

- Approximately 8,350 production Python lines; `config_flow.py` (1,007),
  `switch.py` (603), `utility.py` (563) and `tokens.py` (452) need responsibility
  review, not arbitrary line-count splitting.
- The old devcontainer/VS Code bootstrap installed unpinned HA/tools and omitted
  the qualified native profile. Old editor test collection crossed standalone,
  native and plugin-patched acceptance boundaries.
- Root Ruff excluded the distributable component. CI used explicit Core-policy
  commands, but the editor's ordinary default invocation could skip production
  sources. F01 makes the repository default Core policy and scopes legacy
  tests/tools separately, with no policy file shipped inside the component.
- A previous Pylint override masked `hass: Any`; PR238 fixed the annotations and
  added an actual negative control. Preserve that strict guard.
- Existing lifecycle and SDK-fallback failures escaped high coverage because
  fixtures bypassed real producers/restoration. F05 prioritizes those contracts.
- README onboarding still mentions IE/legacy account web pages; those instructions
  need verification before claiming current support. Manifest release version and
  feature history need an explicit release process, not an automatic version bump.
- Current stock hassfest conflicts with built-in EZVIZ's SDK pin. The narrowly
  documented custom-only exception is not upstream acceptance/coexistence proof.

## Change discipline

Every slice lists its intended contracts, evidence and remaining limits. Preserve
entity/action identities, credential ownership and migration paths. Do not mix
new device features with structural changes. Keep this file and the execution
tracker consistent; an open PR is **review**, not merged or deployed work.
