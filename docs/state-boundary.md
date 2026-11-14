# SDK / Home Assistant state boundary

Baseline: merged PR #241 (`26a7dd9`), published `pyezvizapi` 2.2.0,
HA 2026.10.4 and 2026.11.0. F04 consolidates adapters, not vendor behavior.

## Accepted state and projections

| View | Owner / accessor | Consumers and constraints |
| --- | --- | --- |
| Immutable accepted state | SDK `DeviceSnapshot`; `coordinator.device_state(serial)` / `devices_of_kind(kind)` | SDK owns family rendering, online parsing, capabilities and verified detector booleans. Selection is cached, not a network query. |
| Per-device freshness | SDK `DeviceHealth`; `coordinator.device_is_current(serial)` | Both `present` and `current` must hold. Retained old rows do not establish current health. |
| Detached compatibility dictionaries | SDK `snapshot.as_dict()`; `coordinator.device_data(serial)` | Retain vendor camera fields without inventing duplicate HA models. All entity bases and diagnostics use this accessor. |
| Camera-only push overlay | `coordinator.data`, selected first by `device_data` | Deep-copy replacement for motion/alarm/image observations. It does not mutate the typed snapshot or non-camera inventory, create devices, or clear a failed account poll. |
| Entity availability | `availability.device_available` | Accept only a non-closing coordinator, successful account poll, current/present health, and SDK `state.online is True`. Unknown state is not available. |

A successful shared poll accepts its snapshot and detached views together. SDK
2.2.0 starts subsequent polls from prior device rows: disappearance or a supported
renderer failure retains the prior row and marks its health stale/missing. Do not
simulate that path by deleting `coordinator.data` directly. Full poll failures
retain the accepted cache but HA marks the shared update unsuccessful. A later
successful poll restores availability; a partial push does not.

Shutdown revokes availability even if the last poll succeeded. The cache merge
rejects callbacks after `begin_shutdown`; existing worker/session ownership and
push transport cleanup remain unchanged. This is not a claim that every external
push event or physical camera operation is cancellable.

## Platform inventory

- Camera, image, camera switch/light, number/select/button, update and camera
  sensor/binary-sensor adapters retain compatibility fields and their existing
  capability gates. The SDK has not typed every vendor camera property.
- Bulbs and plugs consume SDK `LightState` / `PlugState` power properties, with
  explicit type narrowing. Commands remain SDK operations using cached metadata;
  successful command acceptance requests a refresh, not optimistic local state.
- Detectors use SDK battery/diagnostic properties and only SDK-verified contact
  or water booleans. Unknown vendor enums do not become truthy HA readings.
- Discovery, command helpers and family factories now declare coordinator,
  config-entry, device-kind and SDK-state types instead of unbounded `Any` at
  those boundaries. Unique IDs, registry naming and discovery remain unchanged.
- Diagnostics uses the same projection accessor and SDK online property. With
  no accepted typed snapshot, `online` is unknown (`null`), even if a legacy
  dictionary contains a status code; allowlisted raw details remain inspectable.
  The schema, redaction rules and normal accepted-cache exports are unchanged.

## Offline evidence

`tests/runtime/core_policy/test_state_boundary.py` mocks account acquisition,
not SDK rendering, typing or stale-row retention. Its controls cover:

- Ten status inputs across cameras, detectors, bulbs and plugs, using the real
  published SDK and real HA coordinator; selectors make no additional request.
- Real inventory disappearance plus renderer failure, retained rows, independent
  device health, failed account poll, partial push and successful recovery.
- Detached camera push projection, unchanged immutable snapshot, unknown serial
  rejection, shutdown availability and rejection of a late cache merge.

Both shutdown-specific controls fail against immutable baseline `26a7dd9`
(late overlay replacement and still-available accepted state), then pass with the
fix. The other controls qualify preserved or newly explicit boundary contracts;
they are not claimed as pre-existing bug reproductions.

Existing acceptance still covers platform discovery/identities, zero/unknown
readings, command metadata and post-command polling. Test fixtures that previously
contained an empty typed device map alongside a healthy camera dictionary now
carry an actual SDK `CameraState`; the HTTP-fallback plug fixture includes the
observed online status in its typed state. The cache-only diagnostics acceptance
control now uses the real coordinator rather than a duck-typed namespace.

This work changes no device permissions, vendor routes, credential ownership,
flow IDs, entity/action identities, entry migrations, SDK requirement or manifest
release version. Native tests deny socket/DNS IO. Passing them is not live cloud,
push-outage or physical-device qualification.
