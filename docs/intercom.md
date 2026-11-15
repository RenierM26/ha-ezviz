# Non-streaming video intercom support (pending SDK release)

This change depends on the companion pyEzvizApi intercom API. The planned SDK
minimum is **2.5.0**, which is not published by this PR. Keep this HA PR in draft
until that release is published and the exact published SDK is qualified using
all standard repository checks. Local candidate tests are not PyPI acceptance.

## Entities

| Device / observed capability | Entities |
| --- | --- |
| `BDoorBell`, local-relay capability 662 == 1 and one valid Video route | Unlock door and unlock gate buttons |
| Outdoor intercom or advertised `DBScreen`, reported `ChimeMusic` fields | Doorbell/motion chime switches and chime volume 0–7 |
| Reported routed nameplate property or legacy switch 611 | Nameplate-light switch |
| `BDoorBell` | Doorbell-ring and lock-activity event entities |

Monitors use their own vendor-reported serial, registry identity and availability.
They are not cameras. No serial splitting manufactures a monitor or relationship.
The existing `<serial>_alarm_volume` unique ID is retained for chime volume, so an
existing intercom volume entity is not duplicated. Ordinary doorbells without the
local-relay capability retain the existing generic volume behaviour.

Unlock buttons are momentary commands. They do not imply that a door is open or
that a physical lock remains unlocked. There is no automatic fallback to the
other relay. Missing/ambiguous capabilities fail before any command is sent.

Settings use the existing account-owned write serialization and delayed refresh.
Accepted sibling fields are preserved for subsequent command construction only;
the UI continues to show observed cloud state until the vendor reports a change.
No missing setting is recreated from the command cache.

The SDK supports caller-known ringtone IDs, but these entities do not invent a
universal 0–15 catalogue. Reported volume, speaker percentage and microphone gain
remain distinct. Unknown features/permissions are not converted into writable
settings.

## Events

The `event` platform exposes:

- **Doorbell**: `ring`, confirmed numeric code 2701.
- **Lock activity**: `lock_unlocked`, reported numeric code 10242.

Event attributes include the vendor event ID, source UTC timestamp, channel and
qualified method (`rfid`/`monitor`, otherwise unknown). They never infer a specific
person/card, arbitrary unlock method or household/button identity from a translated
message. They are occurrences, not physical door/contact state.

The first successful history window per device is a baseline and is not replayed
as new activity. Push/history messages share a bounded 1024-ID deduplication cache.
Events older than five minutes, more than 30 seconds in the future, or without a
usable source timestamp do not emit. Unknown push times do not poison later
history enrichment. Source strings without offsets use only a valid explicit IANA
camera timezone, resolved in the poll worker; missing/invalid/unsupported timezone
metadata never falls back to HA's timezone or UTC. Numeric history timestamps can
still provide the event.

Image-less pushes work. Ringing/unlock events do not generate motion activity.
Existing raw `ezviz_push_event` compatibility remains unchanged.

One opt-in SDK history read retrieves at most 50 recent messages per normal poll.
This is bounded best-effort recovery, not guaranteed lossless backfill across
outages/high traffic. A non-auth history failure does not discard device inventory.
MQTT remains the SDK's cloud transport, not local-only events.

Subscriptions are revoked on entity removal/registry rename and account unload.
There is no extra event polling loop, background prewarm or device preview.

## Explicit exclusions

No streaming acquisition/delivery/RTSP changes, talkback, answer/hangup actions,
credential enrolment, gate-event code guessing or multi-household identification.
Existing camera behaviour is unchanged; this does not make an intercom RTSP stream
available. New physical hardware acceptance is still needed across models/firmware.

## Validation

Use an isolated environment with the exact companion SDK candidate on `PYTHONPATH`
for development tests. This is source-candidate qualification only; the installed
SDK version string alone does not prove the new methods are in a published wheel.
After SDK publication, remove the source override and run the ordinary
`tools/check.py` profiles with the new exact published pin.

The network-denied native suite covers real HA event/registry rename and unload,
monitor identity/health, numeric event decoding and replay/freshness rules,
unknown-time enrichment, single-relay failures, label-light rejection, chime sibling
preservation and observed-only UI behaviour. SDK protocol/evidence details live in
[the SDK documentation](https://github.com/RenierM26/pyEzvizApi/blob/main/docs/intercom.md).
