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

## Lens selection

The **Lens** dropdown replaces the raw Camera resource and Resource index fields:

- **Automatic** (default): use the app's verified direct-control route, or the
  camera's only reported point-control video channel.
- **Lens 1** or **Lens 2**: select that camera's reported video channel 1 or 2.
  These labels do not identify a fixed, upper, lower or moving lens.

For a multi-lens camera, choose a lens explicitly. If Automatic cannot choose
uniquely, the action asks you to select Lens 1 or Lens 2 rather than guessing.
An unavailable or ambiguous selected lens fails without trying another lens.

```yaml
action: ezviz_cloud.ptz_control_coordinates
target:
  entity_id: camera.example
data:
  x_axis: 0.5
  y_axis: 0.5
  lens: "2"
```

For example, a camera reporting `Video/1` and `Video_1/2` uses those exact pairs
for Lens 1 and Lens 2. The user supplies only the lens choice; the integration
resolves the matching pair from current camera metadata. Ordering of metadata
does not change the selection, and a disappearing channel does not renumber the
remaining lens. Channels are not inferred from RTSP URLs. A single reported
zero-based channel is still usable with Automatic; explicit dropdown choices
require reported channel 1 or 2. Other channel layouts are not manufactured or
claimed to be supported by these choices.

Trial automations using the earlier, unreleased `resource_identifier` and
`local_index` fields must remove both and use `lens: "1"` or `lens: "2"` instead
when selection is necessary. Old fields fail validation rather than being
silently ignored. Existing released camera actions are unchanged.

## Capability requirements

The retained app has two distinct paths:

| App support / route | HA behavior |
| --- | --- |
| Direct control, support586 exactly 1 | Automatic uses `Video_1/1`, matching app direct tapping; a direct-only camera can also use Lens 1 for that fixed route |
| Point location, support724 exactly 1 | Automatic uses the unique reported Video channel; explicit Lens 1/2 requires a unique matching reported channel |
| Multiple selected-point routes with Automatic | Ask the user to choose a lens before sending any command |
| Missing, malformed or ambiguous selected lens | Fail before a command; never choose the first/other lens or manufacture a route |
| Only generic pan/tilt flags, no point/direct proof | Reject as unsupported |

Support flags are capability hints; the app has additional player-mode/UI
conditions. They do not establish support for every model/firmware or permission.
Cameras must be currently available, not stale, removed or unloading.

No alternative path is tried after an error. On multi-target actions, the same
lens choice applies separately to each camera's reported channels and ordinary
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
