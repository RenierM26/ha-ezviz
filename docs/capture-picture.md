# Capture pictures and snapshot images

`ezviz_cloud.capture_picture` asks the cloud to take a new picture on channel 1.
It does not require RTSP; battery devices may wake for the request. The original
feature was contributed in [#235](https://github.com/RenierM26/ha-ezviz/pull/235).
Multi-lens capture selection is not added by this follow-up.

Example action (replace the entity ID):

```yaml
action: ezviz_cloud.capture_picture
target:
  entity_id: camera.example
response_variable: captured
```

The optional response contains an entity-ID-keyed `picture_url`. The corresponding
Snapshot image entity updates independently of the Last motion image entity.
The signed picture URL is held in memory, not in the image entity's state, and
is not restored after a restart. Treat URLs returned to automations as private.

## Legacy last-image URL sensor

The URL-valued diagnostic sensor `last_alarm_pic` is retired in this unreleased
line; use the existing **Last motion image** entity for the most recent alarm
image, or **Snapshot** for an explicitly requested capture. This does not remove
the internal image source URL or change image entity identities. See the
[consumer migration and registry-retirement notes](upgrading.md#breaking-change-legacy-alarm-image-url-sensor-removed).
Do not publish a signed source URL as a substitute sensor state/attribute.

## Download ordering and errors

Each capture advances an image generation, even when the server reuses a URL.
An older pending download is discarded if a newer capture arrives: it neither
returns stale bytes nor poisons the newer cache. Each image request performs at
most one download; the retired request returns no image, and the request for the
current capture can load its bytes. There is no automatic refetch loop.

Malformed capture results, missing/non-string URLs and non-HTTP(S)/hostless URLs
raise a controlled action error without publishing a snapshot update. Failed
image downloads are not cached. Existing JPEG loading/decryption and stable
entity/service identifiers are retained.

## Worker lifecycle and SDK requirement

Capture calls and image decryption/JPEG processing use coordinator-owned executor
jobs. This includes the alarm-image HTTP proxy. Jobs are shielded from caller
cancellation, tracked before dispatch, and joined during HA's integration-stop
phase. Normal unload retains cleanup after its foreground wait; late results
cannot publish after shutdown begins. HA's forced-shutdown deadlines still apply;
Python cannot terminate an arbitrary stuck OS thread.

The manifest requires published `pyezvizapi>=2.3.0` for shared inventory and
channel-99 migration. All CI runtime jobs qualify the actual published 2.3.0 minimum.
The earlier bounded 1.x dependency from #237 is intentionally superseded here.
HACS requires HA 2026.10.4 or newer, matching the tested baseline.

The 24 original snapshot acceptance controls are retained alongside the inventory,
config and push acceptance controls in pinned HA 2026.10.4. Native-runtime checks
also run against HA 2026.10.4 and 2026.11.0, without the HA pytest plugin or a
forced override of its dependencies. The native fixtures initialize real entries,
registries and disk storage, and use actual `HomeAssistant.async_stop()`; network
boundaries remain synthetic. They do not establish production boot, real camera
captures, broker delivery or player/browser compatibility.

```sh
python -m pip install -r tests/runtime/requirements.txt homeassistant==2026.11.0 pyezvizapi==2.3.0
python -m pytest --asyncio-mode=auto -o asyncio_default_fixture_loop_scope=function tests/runtime
```
