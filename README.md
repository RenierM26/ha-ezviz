# EZVIZ Cloud for Home Assistant — Ezviz(Beta)

[![HACS custom integration](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![Donate](https://img.shields.io/badge/donate-Coffee-yellow.svg)](https://www.buymeacoffee.com/renierm)

A custom Home Assistant integration using [pyEzvizApi](https://github.com/RenierM26/pyEzvizApi)
for cloud inventory, optional channel-99 push, camera images and local RTSP sources.
The HA integration is named **Ezviz(Beta)**; its domain is `ezviz_cloud`.
It is separate from Home Assistant's built-in **EZVIZ** integration.

## Release status

**This README describes unreleased `main`.** The latest published integration
release checked on 2026-11-14 is `0.2.0.25`; `main` still carries that manifest
version but includes subsequent, untagged changes. Installing that published tag
does **not** install the overhaul described here. Use the selected release's own
documentation for its behavior. A new manifest version/tag and explicit release
approval are still required; no release is implied by merging these PRs.

For this development line:

| Requirement | Qualified baseline |
| --- | --- |
| Home Assistant | 2026.10.4 minimum; native CI also tests 2026.11.0 |
| Python | 3.14.2+; HA manages Python on OS/Container installations |
| SDK | Published `pyezvizapi>=2.3.0`; CI qualifies exactly 2.3.0 |
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
- Camera options select local RTSP credentials and path. This work adds no new
  streaming protocol or universal battery-camera RTSP support.
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
