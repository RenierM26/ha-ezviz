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

Fresh credential/MFA login uses the required `enable_channel99()` API.
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
the existing runtime intact. All shared-client actions participate in coordinator ownership: camera motion
and wake, buttons/PTZ, switches/lights/sirens, account alarm actions/status,
numbers/selections/text, media alarm metadata and Options Flow SDK calls.
Services use HA's async methods; cancelled callers cannot cancel their executor
workers. HTTP closure drains both polling and action futures. Fresh credential
clients and push startup/cleanup keep their separate explicit lifecycle owners.
Entity and service IDs remain unchanged.

Diagnostics use only cached allowlisted scalar/health fields and credential-free
push health; they do not fetch the account again or include full token/session,
image URL, raw vendor payload, serial-keyed dictionaries or local credentials.

## Published SDK requirement and release handoff

The manifest requires **pyezvizapi >=2.0.0**. The
[published 2.0.0 release](https://pypi.org/project/pyezvizapi/2.0.0/) contains
[pyEzvizApi PR #427](https://github.com/RenierM26/pyEzvizApi/pull/427), LAN RTSP
controls and the alarm epoch correction. This integration directly uses
`poll_devices()`, typed snapshots, `enable_channel99()`, durable callbacks and
push health diagnostics. There is no obsolete-SDK loader, credential-login or
constructor fallback. Existing config-entry/registry migration and web-profile
reauthentication remain: those migrate user data, not SDK versions.

All CI runtime jobs install **pyezvizapi==2.0.0 from PyPI** to qualify the declared
minimum. Python 3.12 covers standalone lint/types/lifecycle tests; the separate
Python 3.14 job qualifies actual HA 2026.10.4 and disk storage. No editable SDK
checkout, direct-Git manifest requirement or unpublished dependency remains.

The SDK release gate is complete. Review and merge this integration separately;
HA integration publication and production deployment still require their own
authorization. This work supersedes the older channel-99 proposal #234 rather
than merging its obsolete SDK pin and alternate runtime implementation.

## Image CPU and shutdown ownership

Last motion and Snapshot images retain separate, stable entity IDs. Both reject
older downloads after a URL, encryption-key or capture generation update.
Decryption, JPEG scanning, PIL decode/cropping and proxy decryption execute on
owned worker tasks with immutable byte/key/lens inputs, not on HA's event loop.
The image proxy releases its downloaded response and never forwards incoming HA
authentication headers; it refuses requests once the integration is stopping.

An executor call alone is not shutdown ownership. HA can cancel background
futures before its stop event while the underlying threads continue. Critical
worker tasks are tracked before dispatch (non-eager start), shielded from caller
cancellation, retained by their integration owner, and joined through temporary
stop listeners. Completion removes those listeners. Polls, actions, push
connect/health/stop, credential login, token saves and cleanup use this boundary.
Cancelled background push monitors are distinct from their still-owned workers.
The registered integration stop callback awaits completed cleanup rather than
returning at the five-second foreground-unload deadline. The framework's forced
shutdown deadline remains a limit; arbitrary OS threads are not cancellable.

Coordinators receive their config entry explicitly rather than relying on the
ContextVar fallback. HACS declares HA 2026.10.4 as the supported minimum.

## Latest-stable runtime qualification

The full 52-control acceptance suite remains on the matching HA 2026.10.4 test
plugin. A separate 16-control native suite runs on actual HA 2026.10.4 and
2026.11.0, with neither the HA test plugin nor inconsistent dependency overrides.
It covers real atomic/fsynced storage, failures/deferred writes, cancelled token
producers, actual core shutdown, cancelled push/credential workers, late capture
suppression, off-loop image/proxy processing and bounded listener lifetime.

These are isolated API/lifecycle controls, not a production-instance upgrade or
live media/broker/hardware qualification. The unmodified core's HTTP module emits
an aiohttp inheritance deprecation warning in these environments; no integration
warning or validator is suppressed.

## Narrow hassfest exception

Current hassfest compares custom dependencies against every optional core
integration's requirement, even when unused. Core EZVIZ still requests
`pyezvizapi==1.0.5.0`; this custom integration needs the published 2.0.0 APIs.
Our custom-only real-HA acceptance passes, but **do not enable built-in EZVIZ
alongside this integration**: their shared package requirements conflict and
simultaneous operation is not qualified.

The hassfest workflow runs the current stock container and all its validators
through `tools/check_hassfest.py`. It converts only this exact requirements error
for custom `ezviz_cloud`, with the sole requirement `pyezvizapi>=2.0.0`, into a
visible GitHub warning. All other errors remain fatal, including malformed
manifests/requirements, other packages and changed core/SDK version conflicts.
There is no blanket `continue-on-error`, skipped requirements plugin, altered
runtime dependency or replacement core requirement file.

When core accepts 2.0.0, stock validation produces no matching error and the
exception is unused. Remove the wrapper after upstream alignment; review any
changed mismatch rather than widening it automatically.

## Firmware status and plug channels

Plug status selects the explicit device channel0 rather than a later unrelated
outlet. Missing channelNo uses the SDK's documented unscoped fallback. Firmware
availability accepts positive codes; progress remains active through upgrading,
rebooting, waiting and downloading. Existing camera update IDs stay unchanged;
update entities expose `upgrade_status` and `upgrade_phase` as state attributes.
No firmware upgrades are performed by the acceptance tests.

## Reproduce offline acceptance

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/python -m pip install -r tests/acceptance/requirements.txt pyezvizapi==2.0.0
.venv-ha/bin/python -m pytest -p pytest_homeassistant_custom_component --asyncio-mode=auto tests/acceptance
```

Tests use actual HA classes and disk storage in temporary config directories,
synthetic account/device data, and mocked outbound account/command boundaries.
The normal Python 3.12 CI uses that same published minimum; older SDK versions are
not supported by this integration branch.
