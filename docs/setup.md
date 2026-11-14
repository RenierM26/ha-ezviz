# Installation and setup

These instructions describe **unreleased main**, not the older published
`0.2.0.25` release. Check the [release status](../README.md#release-status) first.
UI names below match the integration's English strings; HA/app translations can
vary. No account login or physical-device setup was performed to validate this
rewrite; flow behavior is covered by the native HA tests.

## Before installation

- Use an EZVIZ account that can already see the devices in the current EZVIZ app.
  Register/add/manage devices through that app and current vendor guidance. The
  old account-registration/web-login links are not prerequisites.
- Check your selected release's HA/Python requirements. Main requires HA
  2026.10.4+ and Python 3.14.2+. Do not replace HA OS/Container's managed Python.
- Do not run this alongside built-in EZVIZ: its `pyezvizapi==1.0.5.0` pin conflicts
  with this line's `>=2.4.0` requirement. The narrowly documented CI exception is
  not proof that both can load together. Do not force-install SDK packages into
  HA to hide a dependency conflict.
- For an existing installation, first read [upgrading and rollback](upgrading.md).

## Install a selected release

### HACS custom repository

With HACS already installed, open its menu → **Custom repositories**, add
`https://github.com/RenierM26/ha-ezviz` with type **Integration**, and select the
version you intend to install. Follow HACS's installation/restart prompt.
See [HACS's current custom-repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/).
A published-release install is not an install of untagged main.

### Manual installation

Download the selected tag/commit from this repository. Copy only
`custom_components/ezviz_cloud` into HA's configuration directory at
`custom_components/ezviz_cloud`, preserving that layout. Do not copy the whole
repository as a component or nest another `custom_components` folder inside it.
Restart HA once after installing/replacing integration code. There is no
`configuration.yaml` account block to add.

Testing main manually requires recording the exact commit and accepting that it
is unreleased; do not identify it only by the unchanged manifest version. Installing
files, a HACS download, and restarting HA are distinct from configuring an account.

## Add the cloud account

1. Open **Settings → Devices & services → Add integration → Ezviz(Beta)**.
2. Enter your EZVIZ account login and password. Select the correct account region.
   Presets are **EU** and **Russia**; use **Custom** only with a known account API
   hostname from appropriate vendor/account guidance. The SDK uses HTTPS; custom
   input must not contain a path, port, user information, query or fragment.
3. Timeout defaults to **25 seconds**, accepts integers **1–300**, and bounds
   requests. It does not change the shared **30-second polling interval**.
4. If prompted, complete account two-factor verification. The account password
   and one-time MFA code are not persisted by the integration.
5. Once the initial account poll succeeds, HA loads the entities; push starts in
   the background. Optional push startup is not a reason to withhold all entities.

A shared account can expose a device without granting every command or key-fetch
permission. Availability/capability observations are not permission grants.

## Camera settings

Open the integration entry's **Configure/Options → Camera Settings**, choose a
camera, and review **Camera settings**. The entry must be loaded and not stopping.
**Cloud Settings** contains the account timeout, not camera streaming options.

| Field | Meaning |
| --- | --- |
| Username | Local RTSP username, default `admin`; not necessarily the cloud login. |
| Verification code | Device sticker/verification credential, **not** the temporary SMS/MFA code. Enter a known value or `fetch_my_key` to request it from EZVIZ. |
| Encryption key | Device video/image encryption credential. Enter a known value or `fetch_my_key` to request it. It can differ from the sticker code and cloud password. |
| Use verification code for RTSP | On selects the saved verification credential; off selects the saved encryption key (default). It does not change device encryption or sharing permissions. |
| RTSP path | Path only, default `/Streaming/Channels/102` (commonly sub-stream). `/Streaming/Channels/101` is a common main-stream path, not a promise for every model/NVR. This legacy `ffmpeg_arguments` option is **not** an FFmpeg command line. |
| Test RTSP now | A one-time wake request and RTSP credential probe. Enabled by default for non-battery categories, disabled by default for battery categories; that heuristic does not establish model support. Uncheck it if you do not want that IO. |

The camera IP and playback port come from the device's cached discovery data;
there is no IP/port field in this form. HA builds the RTSP URL from those values,
the selected credential and path. Do not paste a complete credential-bearing URL
into the path field or post one publicly.

**Probe limitation:** the current credential test contacts port 554 with the
SDK's default test URI. It does not use the configured playback path/port and is
not a complete playback/codec/browser test. A successful probe is not proof that
the selected stream will play, and a failed probe need not prove the configured
playback path is invalid. Review model support/network access separately.

Leaving a field as `fetch_my_key` requests that credential; blank is not documented
as an equivalent fetch instruction. Fetches can require separate one-time codes
for verification credential and encryption key. Enter the code for the action
being requested, then review the resulting form and submit it to save. A failed
fetch/validation does not commit partially resolved options; a previous successful
field can be retained as a prefill for review. The test flag and temporary codes
are not persisted. Saving successful options reloads the integration.

Do not disable camera encryption merely to complete setup. There is no guarantee
that every model exposes a fetchable sticker code/key or supports local RTSP.
See [permission and RTSP troubleshooting](troubleshooting.md).

## Where credentials live

- Per-camera credentials/settings are in the cloud entry's options, not ordinary
  readable encryption-key entity state.
- Main keeps one opaque SDK token snapshot in private HA Store and persists a
  stable per-account installation identity before login. Keep the full HA config
  volume persistent across container recreation.
- Private Store is not encryption at rest; treat configuration/backups as private.
  Do not clone token/replay state into concurrently active HA instances.

See [upgrading/storage recovery](upgrading.md) and the
[detailed storage contract](shared-inventory.md#channel-99-and-credentials).
