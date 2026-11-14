# Troubleshooting and support

For the unreleased development line; see [release status](../README.md#release-status).
Separate **cloud polling**, **push**, **local RTSP**, **image decryption** and
**action permission** before changing credentials or restarting devices.

## Start with cached health

Open **Settings → Devices & services → Ezviz(Beta)** and use the entry menu's
**Download diagnostics**. Device diagnostics are also available from a device
page. Downloading uses cached information only: it does not poll EZVIZ, wake a
camera, read private credentials from disk or test a stream.

Check polling success, snapshot age, per-device `present`/`current`/`available`,
push state/fatal category, and pending cleanup. Stale rows can remain after a
missing-device/render failure. `online` or a retained old value alone does not
establish current availability. `null` is unknown, not proof of unsupported
hardware. `verified_binary_state_available` describes a qualified reading's
availability, not the actual contact/water state.

Reports label devices `device_N` locally to the export and omit integration-level
names, serials, addresses, account IDs, credentials, tokens, media URLs, raw vendor
payloads and exception text. HA adds its own system/manifest envelope and filename
(which can include a device's HA name). Review the complete download before
sharing it. An unloaded entry returns minimal runtime status, not fresh health.

## Common symptoms

| Symptom | What it means / next check |
| --- | --- |
| HTTP 504/408 timeout, then normal recovery | Vendor/request timeout. If the next poll/status request succeeds, no restart or reauth is needed. Confirm current health rather than treating one error as permanent. |
| HTTP/API 403 on light/switch/other action | Permission rejection, not automatically expired credentials or proof of a shared device. Check the exact account and action in the EZVIZ app; see below. |
| Polling healthy, no real-time events | Push can be unavailable while polling continues. Polling does not recreate every `ezviz_push_event`. Check push diagnostics separately. |
| Push fatal category `authentication` | One HA reauthentication flow is requested; use HA's prompt for the same account. The cached fatal SDK worker is not retried indefinitely. |
| Push fatal category `storage` | Check HA configuration-volume permissions/free space, then reload once storage works. Login cannot fix a failed durable credential write. |
| Push fatal category `other` | Capture the safe error type/diagnostics, correct the cause and reload; do not repeatedly reauthenticate without an auth indication. |
| Entity unavailable but old values remain | A failed account poll, missing/stale device, unknown/offline status or shutdown can revoke availability without deleting registry identity. Check freshness and wait for a successful poll. |
| Integration still stopping / reload waits | An earlier SDK worker/cleanup still owns the client. Do not force-start a duplicate client or delete storage to bypass it; inspect pending cleanup. |
| API 2009 while fetching a verification credential | Vendor rejected that request. Better error reporting is not proof that this vendor limitation is fixed; check the same account/app capability and report the safe code. |

Push startup retry is an HA-level five-minute retry for nonfatal start failures;
the SDK also has its own session reconnect loop. Fatal failures pause automatic
HA retries. The foreground cleanup wait is bounded, but pending work retains an
owner and completes later; an arbitrary stuck OS thread cannot be forcibly killed
by this integration. A camera reboot is not the default recovery step.

## Shared-account permissions

Try the **same action under the same account** in the current EZVIZ app. If it
fails there too, the owner should inspect sharing permissions under
**Account → Family & Guests → shared device → guest account**, then save the
appropriate permission changes. See [EZVIZ's permission instructions](https://support.ezviz.com.au/support/solutions/articles/154000258681-how-to-edit-shared-permissions).

If it works in the app but HA gets a permission rejection, report the model,
account role, exact action and safe HTTP/API code. That can indicate an SDK
command/routing limitation; do not declare the hardware unsupported or disable
entities based solely on the rejection. SDK-accepted is also not physical-action
confirmation. Never send your guest/owner password, sticker code or key.

## Local RTSP and images

- Confirm that the exact model/firmware supports local RTSP and that HA can reach
  the discovered IP/playback port. A cloud-connected camera is not necessarily
  locally reachable, and battery-camera support is model-specific.
- Check the local username, selected verification/encryption credential and model's
  stream path. Cloud password, sticker code, video key and temporary MFA codes are
  different things. The [one-time probe's limits](setup.md#camera-settings) matter.
- A working stream does not prove the saved image-decryption key is correct;
  capture/last-motion images have a separate path. Capturing can wake a device.
- Do not disable encryption, delete/re-add the integration or repeatedly wake a
  battery camera just to troubleshoot one timeout. Preserve known working options.

## Reporting a problem

Include:

- Integration tag **and exact commit if testing main**, HA version, SDK version
  if known, model/firmware, owner/shared-account role and whether built-in EZVIZ
  is also configured. Manifest `0.2.0.25` alone cannot identify current main.
- The exact failed action and time/timezone, expected behavior, observed HA state
  and any separately observed physical/app outcome. State whether it recovered.
- Polling/push distinction and reviewed diagnostics; keep the safe HTTP/API code
  and exception type. Do not paste entire account responses or RTSP/media URLs.

If diagnostics are insufficient, enable debug logging from the integration menu,
reproduce once, then **disable debug logging** to download the log. Follow
[HA's current logging/diagnostics guide](https://www.home-assistant.io/docs/configuration/troubleshooting/#debug-logs-and-diagnostics).
Logs are not guaranteed to be as tightly redacted as integration diagnostics:
review them for identifiers, URLs and secrets before attaching a minimal excerpt.
Restore normal logging afterward. No global verbose HTTP/payload logging is
needed for an initial report.
