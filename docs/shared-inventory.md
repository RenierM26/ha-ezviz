# Shared inventory and push contract

## Availability and one refresh

Entities require a successful account poll, a present/current device record and a
known online status (vendor code 1). Missing/unknown status, code 2, a failed poll,
or a failed individual device render makes the entity unavailable. A partial
push event does not clear an account-poll failure.

With the new SDK inventory API, one coordinator poll serves every family. Existing
camera platforms keep their camera-only dictionary projection and unchanged
serial-based unique IDs. Non-camera adapters use the same inventory and health.
Temporarily missing devices retain registry identity and stale values, marked
unavailable; newly observed devices/properties are discovered without a restart.
The coordinator owns a timed-out executor worker until it finishes, and does not
start overlapping replacements. The SDK's total request budget bounds its HTTP
work; arbitrary custom blocking hooks still cannot be interrupted by Python.

## Device families

| Family | New HA view | Limits |
| --- | --- | --- |
| Camera | Existing platforms and IDs | No new stream route or optimistic capability inference |
| Detector | Battery percentage and raw contact/water/siren/radio/power diagnostics | Raw enum/radio diagnostics are disabled by default; no assumed radio units |
| Contact / water | Disabled-by-default binary views | Unknown until a model-specific semantic policy is qualified; raw 0/1 is not booleanized |
| Light bulb | On/off light with observed command metadata | No inferred brightness/color temperature scale |
| Smart plug | On/off outlet switch | No inferred power/energy measurements |

The contact/water binary views intentionally remain `unknown` with current SDK
polling. This is not a claim of hardware-qualified detector alarm semantics.
Detector siren control is not exposed. On/off commands use existing public SDK
operations and cached metadata, then request a shared refresh; no optimistic state
is reported as confirmed. No production device actions were used for qualification.

## Channel-99 and credentials

Fresh credential/MFA login uses `enable_channel99()` when the SDK supports it.
Legacy web-profile tokens cannot be migrated by a header change; push asks for
reauthentication while polling continues. Full SDK token snapshots are retained,
including push state; the password and one-time MFA code are not persisted.

The token callback runs on a worker, waits for a private atomic HA Store write,
and only acknowledges after its file/directory fsync completes. HA's ordinary
Store save can swallow disk errors or defer during shutdown: `AcknowledgedStore`
uses the narrowly scoped `_async_write_data` hook to propagate errors and detect
a return without a completed write. This hook is qualified against HA 2026.10.4;
the dedicated real-HA CI must be updated when moving that baseline. A queued
config-entry update is never treated as durable acknowledgement.

Saved snapshots are anchored to the entry's credential identity so reauth does not
restore an old token. Token Store removal waits for late token-producing workers.
The push monitor checks fatal background worker failures, stops the old worker
before recovery, and preserves polling during outages. Runtime cleanup retains
ownership after the five-second foreground unload wait; HTTP closes only after
owned login/action, push and polling workers finish. Reload cannot start a new
client while a previous runtime is still stopping. Failed platform unload keeps
the existing runtime intact. Existing legacy camera action implementations are
unchanged; new inventory actions participate in runtime ownership.

Diagnostics use only cached allowlisted scalar/health fields and credential-free
push health; they do not fetch the account again or include full token/session,
image URL, raw vendor payload, serial-keyed dictionaries or local credentials.

## Release / compatibility handoff

The manifest **still pins released `pyezvizapi==1.0.5.0`**. Feature detection retains
camera polling and the old constructor on that release. New inventory and push
features require a future SDK release containing
[pyEzvizApi PR #427](https://github.com/RenierM26/pyEzvizApi/pull/427), or an explicit
source installation. No nonexistent version or direct-Git manifest dependency is
introduced. The dedicated CI job installs an immutable SDK candidate SHA separately
from the manifest and qualifies this checkout against actual HA 2026.10.4.

Release sequence: merge/release the SDK through the normal owner-approved process;
update the HA manifest to that real release; rerun both legacy/real-HA controls;
then release the integration. Merging source alone does not upgrade production.
Neither release nor deployment is part of this change.

## Reproduce offline acceptance

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/python -m pip install -r tests/acceptance/requirements.txt -e /path/to/reviewed/pyEzvizApi
.venv-ha/bin/python -m pytest -p pytest_homeassistant_custom_component --asyncio-mode=auto tests/acceptance
```

Tests use actual HA classes and disk storage in temporary config directories,
synthetic account/device data, and mocked outbound account/command boundaries.
The normal Python 3.12 CI also checks the released SDK fallback separately.
