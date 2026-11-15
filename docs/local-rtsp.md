# Local RTSP service control

Some camera firmware periodically disables RTSP. The optional **RTSP service**
switch reads and changes that configuration through the SDK's local command port.
It is not a playback test or a new streaming implementation.

## Configure in Camera settings

Open **Settings → Devices & services → EZVIZ Cloud → Configure → Camera settings**
and select a camera. **Local LAN control (HCNetSDK)** is a dropdown on the existing
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

This shared selection is for local LAN device controls, with RTSP service control as
the first consumer. It is **independent of RTSP playback credential**, a separate two-choice
dropdown that retains the existing boolean storage key for compatibility. The legacy/modern wording is a
hint; choose the source accepted by your firmware rather than relying on a model
guess. The initial submission only validates. A service change requires the separate
affirmative confirmation described below.

## Validation and guided RTSP setup

1. Resolve the requested camera credentials, including the existing MFA flow.
2. **Always validate enabled LAN control**, whether or not RTSP testing is checked.
   A failed or unknown LAN result returns to Camera settings with the selected
   source, entered/resolved fields and requested-test flag retained. Nothing is
   saved; select another source or correct a field explicitly. No fallback occurs.
3. If **Test RTSP connection** is checked and the authenticated LAN response
   explicitly reports RTSP off, show **RTSP service is disabled**. The enable
   checkbox is unchecked by default. Declining returns to the previous form;
   configuring LAN control alone never offers or enables RTSP.
4. An affirmative submission permits one enable attempt through the shared local
   executor. The current saved settings are checked inside its lock first; a stale
   offer returns to fresh settings without writing. The operation re-reads current
   service state, changes only RTSP if needed, verifies the ACK and reads back.
5. Only after verified on, run the explicitly requested RTSP credential test and
   save the camera options. Success distinguishes verified service configuration
   from the DESCRIBE result. A successful enable followed by a failed test is
   reported as such; it is not called rollback and does not save the options.

Unknown LAN state, closed ports, timeouts and authentication errors are never
interpreted as RTSP off. Failed/unverified writes return to the form without an
automatic retry. A new explicit test reads actual state again before any new offer.
The offer is consumed before the write, and removal/shutdown invalidates it.

The two authentication dropdowns are independent. **Test RTSP connection** is
unchecked by default; if LAN is disabled it simply runs the existing RTSP probe
without any enable offer. This SDK probe is an RTSP DESCRIBE authentication check
on default port 554—not full playback or proof for every custom port/path. A
failed playback test alone never causes a camera setting to be changed.

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
