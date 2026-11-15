# Local RTSP service control

Some camera firmware periodically disables RTSP. The optional **RTSP service**
switch reads and changes that configuration through the SDK's local command port.
It is not a playback test or a new streaming implementation.

## Configure in Camera settings

Open **Settings → Devices & services → EZVIZ Cloud → Configure → Camera settings**
and select a camera. **Local device controls** is a dropdown on the existing
credential screen:

| Choice | Local-control credential |
| --- | --- |
| Disabled | No local service controls or polling |
| Enabled — verification/sticker code (legacy cameras) | The existing verification-code/password field |
| Enabled — encryption key (modern cameras) | The existing encryption-key field |

The local username is the camera username already on this screen. The address
comes from the SDK's current camera inventory. There is no second password copy,
separate LAN configuration menu, or automatic credential fallback. If the selected
field contains `fetch_my_key`, the existing credential-fetch/MFA flow resolves it
before the local read-only validation. A missing or rejected selected credential
never causes the other credential to be tried.

This shared selection is for local device controls, with RTSP service control as
the first consumer. It is **independent of Use verification code for RTSP**, which
still chooses the credential for stream playback. The legacy/modern wording is a
hint; choose the source accepted by your firmware rather than relying on a model
guess. Saving these options never enables or disables the camera's RTSP service.

**Test RTSP now** is unchecked by default and remains an explicit wake/playback
credential probe. Leave it unchecked to configure local service control while
RTSP is off. The local validation tests the command-port API, not port 554.

## Requirements and limits

- An eligible non-battery camera in the existing account. Only the SDK's `IPC`
  category is offered; battery, doorbell, peephole and unknown families are
  excluded. The camera must then report a recognized RTSP service field.
- A valid, reachable camera IPv4 address in the SDK inventory and **TCP 8000**
  reachable from HA. This is the management port, not the RTSP port (normally 554).
  No automatic port-opening fallback exists.
- The selected camera credential must actually authenticate the local SDK API.
  EZVIZ account credentials are not used for this connection.

The normal options reload adds **RTSP service** to the camera's configuration
entities. Choosing **Disabled** later stops local controls, but leaves the camera's
service setting, stored camera credentials, stream settings and images unchanged.
Existing credentials are still used elsewhere; disabling local controls does not
purge credentials or their history from HA backups. If you installed an earlier
unreleased revision of this PR with a separate LAN form, select a source here;
saving clears that superseded form's copied credential rather than guessing a
source for it. Published installations require no storage-version migration.

## Observed state and writes

- On/off is the last observed service configuration, not playback success.
- A separate coordinator reads every **five minutes** while this entity is active.
  LAN errors affect only this switch; cloud polling failure does not itself mask
  reachable local state. Normal cloud setup is still required to load the entry.
- Each operation resolves the current selected source, key and observed address.
  Later camera-key updates are used without retaining an old credential snapshot.
  Local operations on one camera share an owned executor and lock, including
  future local controls; different cameras have independent locks.
- A change reads current configuration, updates only RTSP, requires an
  application-level ACK and reads back. Other SDK/web/UPnP and unknown fields are
  preserved. An already-matching value causes no write; there is no optimism.
- Unknown/missing state, bad credentials or a closed port make it unavailable,
  not off. A failed ACK/read-back has an unknown outcome. Camera services may
  restart; wait for recovery and refresh (`homeassistant.update_entity`) before
  deciding whether to retry. No automatic write retry or credential rotation.
  The five-second timeout applies per socket operation, not the entire sequence.

You can separately create an opt-in HA state automation that calls
`switch.turn_on` when this switch changes **from on to off**. It reacts after the
next observed local read, without treating `unavailable` as off. An intentional
off will also trigger it, so disable that automation before deliberately switching
RTSP off. There is no hidden watchdog or startup write.

Read-back confirms configuration only—not a listener, authenticated stream,
codec support or HA playback. It cannot add RTSP to unsupported firmware. No new
live-camera validation is claimed by this PR.
