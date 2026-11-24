# EZVIZ Cloud for Home Assistant — Ezviz(Beta)

[![HACS custom integration](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![Donate](https://img.shields.io/badge/donate-Coffee-yellow.svg)](https://www.buymeacoffee.com/renierm)

A custom Home Assistant integration using [pyEzvizApi](https://github.com/RenierM26/pyEzvizApi)
for cloud inventory, optional channel-99 push, camera images and local RTSP sources.
The HA integration is named **Ezviz(Beta)**; its domain is `ezviz_cloud`.
It is separate from Home Assistant's built-in **EZVIZ** integration.

## Release status

This documentation describes **0.4.1**, a patch release that fixes camera
switch routing and lets you remove stale devices from Home Assistant.
For published downloads, use
[GitHub Releases](https://github.com/RenierM26/ha-ezviz/releases).
Read the [0.4.1 change notes](CHANGELOG.md#041) and
[upgrade/rollback guide](docs/upgrading.md) before updating. A merge into
`main` alone does not publish a HACS update.

| Requirement | Qualified baseline |
| --- | --- |
| Home Assistant | 2026.10.4 minimum; native CI also tests 2026.11.0 |
| Python | 3.14.2+; HA manages Python on OS/Container installations |
| SDK | Published `pyezvizapi>=2.8.0`; CI qualifies exactly 2.8.0 |
| Built-in/custom coexistence | **Not supported/qualified:** built-in EZVIZ pins a conflicting SDK version |

Cloud login and initial polling must succeed. Local RTSP requires a model that
supports it and network reachability from HA. Push is optional and independent
of the shared 30-second cloud poll; this is not a local-only integration.

## Start here

1. Read [installation and account/camera setup](docs/setup.md).
2. Review [upgrade, credentials and rollback](docs/upgrading.md) before changing
   an existing installation, especially the write-only encryption-key change.
3. Use [troubleshooting and support](docs/troubleshooting.md) for permission,
   timeout, push or RTSP problems. A transient cloud timeout is not automatically
   a reason to restart or reauthenticate.

## Features and limits

- Existing camera platforms and stable entity/action identities are retained,
  except the [retired legacy image-URL sensor](docs/upgrading.md#breaking-change-legacy-alarm-image-url-sensor-removed).
- [Chime and alarm voice volume](docs/alarm-volume.md) are separate capability-gated
  controls: flag 185 for `ChimeMusic` (0–100; DBScreen guard 0–7), flag 203 for
  `CustomVoice_Volume` (10–100). Neither changes arm/disarm behavior.
- One SDK poll supplies cameras, detectors, light bulbs and plugs. Entity
  availability requires successful polling, current/present device health and a
  known online state; retained stale values are not current measurements.
- Bulbs/plugs expose observed on/off controls, not inferred energy/color features.
  Detector contact/water booleans remain unknown without qualified SDK semantics;
  raw diagnostic readings are not converted to alarm truthiness.
- Cameras with device-wide alarm-light support expose one **Alarm light**.
  The alarm-light capability flag is required; switch rows alone do not prove
  support for device-wide or independent lens control. **Flicker light on movement** remains a separate setting.
- The SD card status diagnostic uses reported disk telemetry from the account
  poll, distinguishing a reported empty slot from unavailable telemetry.
  Capacity remains a separate sensor. This does not add card formatting or
  local recording playback.
- Camera options select local RTSP credentials and paths. To try experimental
  **EZVIZ cloud live view**, open **Configure → Cameras → [camera] → Live playback
  source** and pick **Full HD** (main stream) or **Standard** (substream, where
  supported). Actual resolution depends on the camera.
  The choice applies to all reported lenses of that camera, each using its own
  channel. Local RTSP remains the default; switching back restores the existing
  RTSP paths and credentials. Stills and controls do not switch routes.
  Cloud live view uses HA's HLS player (not WebRTC), needs FFmpeg and cloud
  access, and may fail on unsupported models, sharing permissions, or encrypted
  formats. Encrypted cameras need a saved camera encryption key. Keep HA camera
  stream preloading off for on-demand playback, especially on battery devices.
  There is no automatic fallback between local, cloud, or quality levels.
- Opt-in [local RTSP service control](docs/local-rtsp.md) reads and changes the
  service configuration over TCP 8000 on supported non-battery cameras. A shared
  Camera settings dropdown selects the existing sticker code or encryption key
  for local controls, independently of the playback credential dropdown. LAN
  authentication is always tested when enabled; an explicitly requested RTSP test
  offers an enable confirmation only when that authenticated status is off.
- [Capture-picture/snapshot images](docs/capture-picture.md) work independently
  of RTSP; a requested capture may wake a battery device.
- Cached, allowlisted diagnostics help distinguish polling, push, device health
  and pending cleanup without making another cloud/device request.

See [the detailed shared-inventory and persistence contract](docs/shared-inventory.md)
for exact family, storage, migration and ownership limits. Offline tests do not
establish live broker delivery, physical actions or browser/media compatibility.

## Development and maintenance

- [Contributing](CONTRIBUTING.md), [support](SUPPORT.md) and [private security reporting](SECURITY.md)
- [Repository automation and merge policy](docs/repository-maintenance.md)

- [Development/check commands](docs/development.md)
- [Architecture and ownership](docs/architecture.md)
- [Test responsibilities and isolation](tests/README.md)
- [Release checklist and source preflight](docs/releasing.md)
- [Validation dependency pins](docs/validation-pins.md)
- [Staged overhaul tracker](docs/overhaul-backlog.md)

Report reproducible problems through [GitHub issues](https://github.com/RenierM26/ha-ezviz/issues)
using the [support checklist](docs/troubleshooting.md#reporting-a-problem).
Do not attach passwords, verification codes, keys, tokens or signed media URLs.

## Video intercoms (unreleased)

See [non-streaming intercom support](docs/intercom.md) for monitor inventory,
relay buttons, chime settings and ringing/access events. This branch depends on
the companion SDK release; it does not add streaming or talkback.
