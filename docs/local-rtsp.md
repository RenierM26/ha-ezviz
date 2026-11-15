# Local RTSP service control

Some camera firmware periodically disables RTSP. This opt-in configuration switch
lets you read and change the camera's RTSP service setting using the published
SDK's local command-port API. It is **not** a stream-health test or a new streaming
implementation. No local requests are made by this feature until you opt in.

## Requirements and setup

- An eligible non-battery camera in the existing cloud account. Discovery is
  conservative: only the SDK's `IPC` camera category is offered, excluding battery,
  doorbell, peephole and unknown families. A recognized RTSP setting must then be
  returned by the camera; category alone does not prove firmware support.
- A reachable literal camera IPv4 address and **TCP 8000** from Home Assistant.
  This is the local management port, not the RTSP streaming port (normally 554).
- The camera's **current local device username/password** (normally user `admin`).
  This is not your EZVIZ account password. It may differ from either the sticker
  code or encryption/media key; this feature never guesses or fetches one.

In **Settings → Devices & services → EZVIZ Cloud → Configure**, choose
**Local RTSP service**, select a camera, enable **Manage RTSP service locally**,
and enter its address and local credentials. Setup performs a **read-only** status
query before saving; it neither enables RTSP nor changes any other camera service.
The normal HA options reload adds **RTSP service** to that camera's configuration
entities. Existing RTSP credential selection and stream paths are unchanged.

The password field is masked and is never prefilled with the saved secret. Leave
it blank on a later edit to retain the saved LAN password. Uncheck management to
stop monitoring and remove these LAN credentials from the current options; this
**does not disable RTSP on the camera**. Like other HA configuration secrets,
older HA backups can retain earlier credentials. There is no secret state entity.

## State and writes

- **On/off** is the last observed service configuration, not playback success.
- A separate coordinator reads it every **five minutes** while the entity is
  active. LAN failures affect this switch only, not other cloud entities. Cloud
  polling failure does not by itself make a reachable local switch unavailable;
  the integration still requires its normal cloud setup to load initially.
- Each change reads the current configuration, changes **only RTSP**, requires
  application-level success and reads the setting back. Other SDK/web/UPnP and
  unknown configuration fields are preserved. An already-matching value causes
  no device write. No optimistic state is presented.
- Missing/unknown state, bad credentials or a closed port make it unavailable,
  not off. A failed ACK/read-back also leaves the outcome unknown. Camera services
  may restart during a change; wait for them to recover and refresh the switch
  (`homeassistant.update_entity`) before deciding whether to retry. There is no
  automatic write retry, port-opening fallback or credential rotation. Each SDK
  socket operation has a five-second timeout; a complete read/change/read-back
  can involve multiple operations and is not promised to finish in five seconds.

To deal with firmware disabling RTSP, you can separately create an **opt-in HA
state automation** that calls `switch.turn_on` when this switch changes **from on
to off**. It will react when the next local read observes the change. Using that
specific transition avoids treating `unavailable` as off; deliberately switching
RTSP off will also trigger such an automation, so disable the automation first.
The integration contains no hidden keep-enabled watchdog or startup writes.

Enabling the setting does not prove that a listener is running, authentication
works, codecs are supported or HA playback succeeds. It cannot add RTSP support
to firmware that lacks it. No new live-camera validation is claimed by this PR.
