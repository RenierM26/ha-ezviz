# Configuration-flow modularity (F03)

Baseline: merged ownership PR #240 (`cf0ee87`). This is a responsibility-based
extraction, not a new flow, credential format or authentication implementation.

| Module | Responsibility | Must not own |
| --- | --- | --- |
| `config_flow.py` | Canonical HA account-flow registration; initial login/MFA, reauth, private staging/promotion and reload completion; canonical options factory. | Camera SDK fetch/probe implementation or options UI state. |
| `options_flow.py` | Cloud/camera options forms, pending MFA and prefill state, live runtime availability guards, optimistic-conflict baseline and final allowlisted option patch. | A captured coordinator across steps, vendor HTTP protocol or private token storage. |
| `camera_flow.py` | Camera verification/encryption credential retrieval, field-specific partial-result errors and explicit wake/RTSP probe. Receives the current coordinator and edit baseline for the operation. | Committing options, saving MFA codes or opening unowned SDK clients. |
| `flow_validation.py` | Strict timeout and API-host syntax validation/region resolution shared by account and options flows. | Network IO, custom nonserializable HA form fields or storage. |

`config_flow.py` falls from 1,007 to 400 lines. The options handler remains one
cohesive UI implementation (392 lines), camera preparation is 206 lines and
validation is 67. Some imports are repeated across modules; reducing total line
count is not the goal. No mixin hierarchy or generic flow engine was introduced.

## Preserved interfaces and lifetimes

- HA still imports/registers `EzvizConfigFlow` from `config_flow.py`; its options
  factory returns the same handler class name via the extracted implementation.
- Entry/version, flow-step IDs, translations, form field defaults and serialized
  schemas, MFA routing, option patch allowlist and conflict behavior are unchanged.
- Cloud login still uses the existing durable identity/token owners and executor
  task shielding. Staging/promotion and reauth replacement/reload ownership remain
  in the account flow; this extraction does not move them into UI utilities.
- Camera preparation still uses the coordinator-owned executor, preserving
  partial credentials on field-specific failures, saved-value fallback, explicit
  RTSP opt-in and stripping of one-time fields on success.
- Options resolve the live runtime for each step. The thin preparation method
  passes that coordinator and the current edit baseline to the helper; it does
  not retain another long-lived client/coordinator owner.
- No compatibility aliases for moved private SDK/validation helpers are added.
  The single test transport patch now targets `camera_flow.TestRTSPAuth`, the
  actual owning module. Existing real HA flow/HTTP/MFA/storage tests still run.
- Camera helper logs move under `custom_components.ezviz_cloud.camera_flow`;
  message content/severity stays unchanged and the integration logger covers it.

## Verification scope

Native coverage now explicitly includes **all four flow modules plus diagnostics**,
not just the smaller canonical flow file. On both supported HA versions it remains
100% statements/branches across **513 statements / 86 branch outcomes**. The
increased statement count is module imports/definitions and delegation, not a new
behavioral coverage claim. No `no cover` exemptions or weaker thresholds were added.

All existing 464 native tests run on each supported published HA version and
Core dev, with 52 acceptance and 46 standalone tests. A structural comparison
against the merged baseline also checks unchanged account/options methods,
validation, SDK helper/error construction and credential resolution after
normalizing helper names, explicit owner/baseline parameters and docstring
indentation. This supplements, rather than replaces, the behavioral tests.

There are no intended behavioral changes, new dependencies, migration/identity
changes, live cloud/camera requests or deployment. Exception traceback module
locations naturally change. F02's process-local cleanup and unfinished-flow
reservation limits still apply; see [the lifecycle audit](lifecycle-audit.md).
