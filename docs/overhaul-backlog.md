# Staged repository overhaul

Baseline: merged release-discipline PR #244 (`d4ac599`), 2026-11-14. Work proceeds in reviewable
slices. This backlog records implementation status, not deployment approval.

| ID | Priority / state | Deliverable and exit criteria |
| --- | --- | --- |
| F01 | P0 / merged in PR #239 | Shared check entry points; pinned fast-check requirements and published SDK profile; isolated native/tooling dev environments; default component Ruff discovery; VS Code/CI alignment; architecture and test responsibility docs. Production integration files remain unchanged. |
| F02 | P1 / merged in PR #240 | Audit and regression matrix in [lifecycle-audit.md](lifecycle-audit.md). Close late-setup cleanup, transient private-file deletion and replacement-account identity gaps with real HA/Store controls; preserve existing lifetime anchors. |
| F03 | P1 / merged in PR #241 | Extract account/options/camera-preparation/validation responsibilities from the 1,007-line monolith; canonical `config_flow.py` is now 400 lines. See [flow modularity](flow-modularity.md). Preserve form serialization, step IDs, MFA, options conflict handling, migrations and storage recovery. |
| F04 | P1 / merged in PR #242 | Consolidate typed SDK state selectors, compatibility projections and shared availability; type family adapters; fence late shutdown cache merges. See [state boundary](state-boundary.md). Preserve SDK rendering/retention and camera push behavior; no inferred permissions. |
| F05 | P1 / merged in PR #243 | Enforce separate pytest layers; qualify actual SDK transient-recovery/shutdown behavior; migrate the 13-case push module to strict Core test policy without dropping assertions. See [behavioral contracts](behavioral-tests.md). Other legacy modules remain an explicit incremental maintenance scope, not claimed migrated. |
| F06 | P2 / merged in PR #244 | Replace legacy onboarding with code-audited setup/support/upgrade docs and an explicit unreleased-main status. Add release/source preflight and change notes; pin validation actions and actual images, fix bare-version tag triggers. See [release checklist](releasing.md) and [validation audit](validation-pins.md). No version bump, publication or deployment. |

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

## Remaining explicit limits / maintenance

- Main still carries the old published `0.2.0.25` manifest version. A reviewed
  version/tag/release step is required; completing these overhaul slices does
  not publish them. Source-only downgrade is not a migration rollback.
- The existing RTSP credential probe uses port 554/default test URI, not the
  configured playback path/port. This is now documented accurately, not claimed
  fixed or equivalent to a complete playback test.
- Legacy test-policy migration remains incremental; the F05 push module is not
  a claim that every historical test follows Core test policy.
- Built-in/custom SDK coexistence, live cloud/push/device behavior and browser
  compatibility remain outside the offline qualification claims.

## Post-overhaul regression follow-up

Options saves with an attached camera exposed an incompatible config-entry
listener. The follow-up removes that listener, leaving `OptionsFlowWithReload`
in charge of form changes and coordinator notifications in charge of credential
actions. Four real-manager controls cover changed/unchanged cloud and camera
options. See [test evidence and limits](../tests/runtime/README.md#options-reload-ownership).
Fix implemented in a follow-up PR; review/merge pending. No release or deployment.
