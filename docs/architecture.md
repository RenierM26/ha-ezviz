# Integration architecture and ownership

Baseline: merged ownership PR #240 (`cf0ee87`), HA 2026.10.4+, published SDK 2.2.0+.
This describes current contracts; it is not a proposal to replace working owners.

## Boundaries

| Concern | Owner | Contract |
| --- | --- | --- |
| Vendor authentication, HTTP fallback, push protocol, device rendering | `pyezvizapi` | HA consumes published SDK APIs; no copied vendor routing/auth implementation. SDK success is not proof of physical operation. |
| Initial login/MFA/reauth, options and migration forms | `config_flow.py`, `options_flow.py`, `camera_flow.py`, `flow_validation.py`, `migration.py` | Preserve step IDs, HTTP-serializable forms, config-entry identity, migrations and retryable storage errors. |
| Installation identity, staged login, canonical token snapshot | `tokens.py` | Acknowledge durable private Store writes; retain opaque SDK fields, shared locks and revisions; reject stale writers. |
| Committed per-camera options and key changes | `credentials.py` | Serialize device operation and local commit; reject conflicting edits; do not resurrect removed entries. |
| Account polling, accepted inventory and command executor ownership | `coordinator.py`, `workers.py` | One shared poll owner; retain pending work through cancellation/unload; native tests block network/DNS. |
| Push start, retry, fatal classification and cached event projection | `mqtt.py` | Polling remains independent; authenticate only for actual auth failures; never read lock-held SDK diagnostics on the event loop. |
| Account lifetime and cleanup | `RuntimeCleanup`, `entry.runtime_data` | Stop push and outstanding work before closing HTTP; retain ownership when foreground unload times out or cleanup fails. |
| Entity presentation and actions | Canonical HA platform modules | Preserve unique IDs/action names; use shared availability, cached observations and safe errors rather than guessing permissions. |
| Shared availability and inventory commands | `availability.py`, `inventory.py`, `entity.py` | Offline/missing/stale state is not available; no optimistic plug state update before accepted polling. |
| Images, media and proxy requests | `image.py`, `camera.py`, `media_source.py`, `views.py` | Respect account ownership; execute blocking CPU/network boundaries appropriately; serialize stream creation/removal. |
| Diagnostics and repairs | `diagnostics.py`, `repairs.py`, `errors.py` | Export allowlisted cached metadata without secrets; permission/timeouts do not automatically imply reauth or unsupported hardware. |

## Startup and shutdown

1. Setup consults retained cleanup ownership. It must not create another client
   while a previous owner's cleanup remains incomplete.
2. Load private credentials and stable installation identity; create the SDK
   client, coordinator and cleanup owner; attach `entry.runtime_data`.
3. Validate authentication and perform the first shared refresh. Forward HA
   platforms, then start push in the background. Setup failures stop owned work.
4. Successful unload stops the runtime after platforms unload. The foreground
   wait is bounded, but pending work/cleanup still has an owner.
5. Removal waits for token-producing work to finish before deleting private
   credentials. Transient deletion failures retain the same critical task and
   persistence owner for retry. Shared account identity used by a replacement
   cloud entry is not deleted by old-entry cleanup.

`hass.data` exceptions are intentional and line-scoped: instance-wide view
registration, retained `_runtimes`, pre-entry `_identity_stores`, and shared
`_token_stores`. Initial flows do not yet have `entry.runtime_data`; retained
locks/revisions and cleanup outlive its clearing. Platforms and request handlers
use `entry.runtime_data` for normal entry-owned state. Do not remove these anchors
without a replacement lifetime contract and race regressions.

## Entity lifetime is not account lifetime

HA can remove and re-add the same entity object on a registry rename. Camera
removal fences must reset on add; stopped streams are discarded under their
lifecycle lock and recreated with current preferences. Siren timers retain their
original monotonic expiry across re-add, not a new full minute. Cancelled/queued
callbacks and commands from a prior lifetime must not write to re-added entities.

## Refactoring guardrails

- Keep `ezviz_cloud`, config-entry identity/version migration, entity unique IDs,
  action/event names, option keys and private credential ownership stable.
- Keep canonical HA entry points in their expected files. Extract cohesive
  internal helpers rather than hiding every platform in a generic factory.
- Test actual producer contracts: SDK 2.2.0 retains stale camera projections;
  setting `coordinator.data = {}` is not evidence of inventory disappearance.
- Test real restoration/registry transitions, not only mocked add/remove hooks.
- Do not broaden the documented built-in EZVIZ dependency-conflict exception.
- Report configured, SDK-accepted, HA-reported and physically observed outcomes
  separately. Offline qualification does not establish live cloud/device behavior.

See [flow module boundaries](flow-modularity.md), [lifecycle audit and evidence](lifecycle-audit.md), [overhaul backlog](overhaul-backlog.md) and [test responsibilities](../tests/README.md).
