# Multi-lens cameras

This branch requires the **planned SDK 2.6.0 channel API**, not published SDK
2.5.1. Keep the HA PR draft until SDK publication and ordinary PyPI-only
qualification. A local candidate wheel is development evidence, not a release.

## Entities and compatibility

One physical camera remains one HA device and one account poll/session. Existing
camera IDs, configured streams, last-motion images, Snapshot images and shared
settings are preserved. Unambiguous SDK multi-channel inventory adds **Lens N**
cameras, **Lens N snapshot** images and **Lens N notification** events under that
device. A count alone does not create a lens. Missing channels become unavailable;
list order does not renumber entities. Native registry identities allow a healthy
remaining lens to return after restart while its sibling is absent.

The SDK owns resources, capabilities, routing, image framing and channel
normalization. HA owns entities, options, translated errors and existing worker
lifetime. Battery, network, firmware, siren and device settings remain shared;
no per-lens detection or recording state is inferred from global state.

## Streams and images

Use **Configure → Lens streams**, select a lens label and enter its independently
verified RTSP **path**. Shared credentials and host/port are reused. No complete
URL, host override or credentials belong in this field. Saving uses native options
reload and conflict protection, without probing or moving the camera. Clearing
the path disables that lens stream; the original camera stream is unchanged.

Cloud numbers do not prove RTSP paths: Lens 2 never manufactures
`/Streaming/Channels/201`. Combined-feed models are not claimed to expose separate
streams. Without a path, a lens camera can show its channel-indexed alarm image
or explicitly channel-tagged push image. Unindexed multi-lens pictures are not
assigned by guessing. Original positional/stacked last-motion entities remain
for compatibility. Missing image channels never display another lens.

Concurrent lens viewers share download and SDK batch decoding. Successful stills
are reused until URL, source generation or key changes; stale results cannot
publish after key/target changes or shutdown. No stream is prewarmed.

## Actions and notifications

- Target a Lens camera for **Move to image position**, leaving Lens on Automatic.
  The SDK binds the exact resource; conflicting explicit Lens choices fail.
- **Capture picture** on a Lens camera uses its reported channel and updates only
  that Lens snapshot. The original camera/capture and Snapshot remain compatible.
  Returned signed URLs are private; acknowledgement is not physical verification.
- Use the original camera for shared defence settings. Lens targets reject
  apparently scoped changes that would actually affect the whole device.
- Lens event entities represent receipt of channel-tagged push notifications,
  not inferred persistent motion/recording state. Vendor IDs are deduplicated in
  one bounded account cache. Unknown channels are not assigned; no-ID messages
  can update a scoped image but do not invent an event. Attributes exclude URLs.

## Alarm media

Original device folders remain. Lens folders show only explicitly channel-matched
alarms; unscoped history stays in the device folder. Images are bound to their
lens in the existing bounded opaque-reference registry. The authenticated proxy
uses SDK image selection while retaining public-only DNS/connection checks,
redirect limits and header isolation. Forged scopes cannot reuse another lens's
registered image or create arbitrary external requests.

## Qualification limits

Local candidate-wheel qualification: SDK 6,635 tests (91.70% coverage); HA
1,176 native tests on each of 2026.10.4, 2026.11.0 and Core dev, 52 acceptance
tests and 103 standalone tests. Lint, types, Core policy, translations, scoped
hassfest and dependency consistency checks pass. All installed SDK source files
match the candidate wheel. Its private build metadata reserves version 2.6.0;
this is not a PyPI-published release or ordinary published-wheel qualification.

Native tests exercise actual HA platforms, registries, actions, options reload,
privacy and shutdown, with synthetic SDK data/serialization/images and denied
socket/DNS transport. Physical fixed/moving or upper/lower mapping, independent
feeds, real capture/playback/audio and motor movement require separate bounded
model/firmware qualification. No production deployment or live movement is
performed by these checks. Other settings remain shared until their channel
protocol is verified; this is not a promise of identical features on every lens.
