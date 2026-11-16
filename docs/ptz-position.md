# Point-based PTZ action

The new `ezviz_cloud.ptz_control_coordinates` action uses the existing SDK point
function, corrected in [SDK #446](https://github.com/RenierM26/pyEzvizApi/pull/446).
It requires **published SDK 2.5.1 or newer**. The ordinary development and CI
profiles qualify the exact 2.5.1 PyPI wheel, without SDK source overrides.
The action is not in integration 0.3.0; a merged PR is not a HACS release.

## Usage

Choose **Ezviz(Beta): Move to image position** in HA's action picker and select
one or more of this integration's camera entities. Normalized image coordinates
are numeric values between **0 and 1**, inclusive:

- `x_axis`: 0 is the left edge; 1 is the right edge.
- `y_axis`: 0 is the top edge; 1 is the bottom edge.
- `(0.5, 0.5)` denotes the image center, **not** a repeatable absolute motor angle.

```yaml
action: ezviz_cloud.ptz_control_coordinates
target:
  entity_id: camera.example
data:
  x_axis: 0.25
  y_axis: 0.75
```

No RTSP viewer or stream is started by the action. It does not create a panorama,
read a motor position, wake/enable other features, or add a polling loop. The
command is a single cloud IoT action; acknowledgement is not proof of physical
arrival. Generic directional PTZ support alone is insufficient.

## Capability and resource selection

The retained app has two distinct paths:

| App support / route | HA behavior |
| --- | --- |
| Direct control, support586 exactly 1 | Default `Video_1/1`, matching app direct tapping |
| Point location, support724 exactly 1 | Use the unique reported Video resource/index, or explicitly select a reported pair |
| Multiple, missing or malformed selected-point routes | Fail before a command; never choose the first/other lens or manufacture a route |
| Only generic pan/tilt flags, no point/direct proof | Reject as unsupported |

Support flags are capability hints; the app has additional player-mode/UI
conditions. They do not establish support for every model/firmware or permission.
Cameras must be currently available, not stale, removed or unloading.

For a camera advertising selected-point support, the optional advanced fields
`resource_identifier` and `local_index` select a specific **reported** pair.
Both must be supplied together. They are not derived from the RTSP channel:

```yaml
action: ezviz_cloud.ptz_control_coordinates
target:
  entity_id: camera.example
data:
  x_axis: 0.5
  y_axis: 0.5
  resource_identifier: Video
  local_index: "0"
```

That example requires an actual reported `Video/0` pair; it is not a universal
route. A direct-only camera cannot use arbitrary selected-resource routes.
No alternative path is tried after an error. On multi-target actions, ordinary
HA per-target dispatch applies; there is no atomic transaction across accounts.

## App verification and limits

Retained Android **7.4.1.0421 / code1676**, including Java and raw smali, confirms
normalized X/width and Y/height without inversion, the direct vs selected-resource
paths, and `PTZManualCtrl/CtrlPTZ3DPosition`. The point object includes the full-
unit rectangle and `zoomType: "zoomIn"`; SDK 2.5.0 omitted the latter field.
This is a confirmed app-contract mismatch, not evidence that a particular camera
fails without the field. See the SDK's point-control documentation/PR for exact
provenance. No latest-app, iOS or universal firmware compatibility is claimed.

Tests use synthetic data, real HA dispatch and the published SDK 2.5.1 serializer
with socket/DNS denial. Physical positioning, app UI and hardware behavior remain
unverified. No live PTZ commands, deployment, HA reload or release is performed by
these checks. No existing IDs/options/storage formats or streaming behavior change.
