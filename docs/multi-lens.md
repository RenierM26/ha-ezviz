# Multi-lens cameras

This branch requires the **published SDK 2.6.0 channel API**. The integration
installs the SDK from PyPI; the runtime test profile pins it to 2.6.0.

## Entities and compatibility

One physical camera remains one HA device and one account poll/session. Existing
camera IDs, configured streams, last-motion images, Snapshot images and shared
settings are preserved. A single-lens camera keeps only its original camera
entity. On a multi-lens camera the original entity represents Lens 1; only
additional lenses get new camera and Snapshot entities. The existing Snapshot
serves Lens 1. **Last notification** is available for each reported lens on
both single- and multi-lens cameras.
A count alone does not create a lens. Missing channels become unavailable;
list order does not renumber entities. Native registry identities allow a healthy
remaining lens to return after restart while its sibling is absent.

The SDK owns resources, capabilities, routing, image framing and channel
normalization. HA owns entities, options, translated errors and existing worker
lifetime. Battery, network, firmware, siren and device settings remain shared;
no per-lens detection or recording state is inferred from global state.

## Streams and images

Under **Configure → Camera Settings**, pick the camera and open **Lens streams**.
Lens 1 is the original camera. It keeps its saved path or defaults to
`/Streaming/Channels/102`. Lens 2 on the H9c defaults to
`/Streaming/Channels/202`. Choose **Main stream** for `101` or `201`, **Custom path**
to enter a different path, or **Stills only** to disable RTSP playback for that lens. This does not disable EZVIZ cloud playback. Additional lenses on other models
default to stills unless a custom path is supplied; cloud channel numbers alone
do not establish separate RTSP feeds.

Custom paths begin with `/`; the host, port and credentials are shared. Saving
does not probe or move the camera, and does not change the original camera
entity's identity or shared credentials. Single-lens cameras use the same **Lens streams** menu and show only Lens 1.

Without a path, every lens camera (including the original) can show its latest
verified alarm image. If none is available, there is no preview. Viewing a
still does not open RTSP or take a fresh picture; use **Capture picture** to
update the separate Snapshot entity. A newer polled image can replace a
pushed still only if it contains the target lens. Unindexed multi-lens pictures are not
assigned by guessing. Original positional/stacked last-motion entities remain
for compatibility. Missing image channels never display another lens.

Concurrent lens viewers share download and SDK batch decoding. Successful stills
are reused until URL, source generation or key changes; stale results cannot
publish after key/target changes or shutdown. No stream is prewarmed.

## Actions and notifications

- Target a Lens camera for **Move to image position**, leaving Lens on Automatic.
  The SDK binds the exact resource; conflicting explicit Lens choices fail.
- **Capture picture** on the original camera updates the existing Snapshot for
  Lens 1. On an additional Lens camera, it uses that lens's reported channel and
  updates only the corresponding Lens Snapshot.
  Returned signed URLs are private; acknowledgement is not physical verification.
- Use the original camera for shared defence settings. Lens targets reject
  apparently scoped changes that would actually affect the whole device.
- **Last notification** shows when Home Assistant received the latest
  channel-tagged push, even without an image. Its attributes include the vendor
  alert code and source time when supplied, not image URLs. It does not indicate
  ongoing motion or recording. Vendor message IDs are deduplicated in one bounded
  account cache. Pushes without a known channel are not assigned to a lens;
  no-ID messages can update a scoped image but do not fire the entity.

## Alarm media

Original device folders remain. Lens folders show only explicitly channel-matched
alarms; unscoped history stays in the device folder. Images are bound to their
lens in the existing bounded opaque-reference registry. The authenticated proxy
uses SDK image selection while retaining public-only DNS/connection checks,
redirect limits and header isolation. Forged scopes cannot reuse another lens's
registered image or create arbitrary external requests.

## Qualification limits

Historical pre-release candidate-wheel qualification: 6,635 SDK tests (91.70%
coverage), 1,176 native tests on each of HA 2026.10.4, 2026.11.0 and Core dev,
52 acceptance tests and 103 standalone tests. Lint, types, Core policy,
translations, scoped hassfest and dependency consistency checks passed. All
installed SDK source files matched that private wheel, which was not published.
The final [2.6.0 PyPI wheel](https://pypi.org/project/pyezvizapi/2.6.0/) was qualified
separately by [published-wheel CI](https://github.com/RenierM26/ha-ezviz/actions/runs/44925297925).

Native tests exercise actual HA platforms, registries, actions, options reload,
privacy and shutdown, with synthetic SDK data/serialization/images and denied
socket/DNS transport. Physical fixed/moving or upper/lower mapping, independent
feeds, real capture/playback/audio and motor movement require separate bounded
model/firmware qualification. No production deployment or live movement is
performed by these checks. Other settings remain shared until their channel
protocol is verified; this is not a promise of identical features on every lens.
