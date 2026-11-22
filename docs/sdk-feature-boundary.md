# SDK feature boundary migration

The [manifest](../custom_components/ezviz_cloud/manifest.json) currently requires
`pyezvizapi>=2.6.0`, and CI and local check profiles use the exact published version
in [`requirements/sdk.txt`](../requirements/sdk.txt) (`pyezvizapi==2.6.0`).
See [Development and checks](development.md) for the current setup. No editable
checkout, source override or direct-Git dependency is required.

The [original feature boundary migration](https://github.com/RenierM26/ha-ezviz/pull/253)
used [published SDK 2.4.0](https://pypi.org/project/pyezvizapi/2.4.0/) as both the
manifest minimum and the exact qualification pin. That release contained the
extraction [SDK #438](https://github.com/RenierM26/pyEzvizApi/pull/438) and the focused-module
refactor [SDK #439](https://github.com/RenierM26/pyEzvizApi/pull/439), plus
length-delimited image framing [#441](https://github.com/RenierM26/pyEzvizApi/pull/441)
and app image variants [#442](https://github.com/RenierM26/pyEzvizApi/pull/442).
Pillow is an explicit Home Assistant Core dependency, so the SDK image
helpers use HA's provided Pillow rather than adding another conflicting pin.

## Ownership

The SDK owns:

- Chime/voice/microphone capability, range, decoding and field-preserving writes.
- Port-security read/modify/write, enclosing enable hints and cached vendor fields.
- `SupportExtView` matching and vendor feature/network/storage readers.
- Linked-tracking nested payloads and commands; encoded intelligent-app readers.
- JPEG marker scanning, embedded-thumbnail handling, decryption and stacked-lens splitting.

HA imports the canonical SDK modules. Its `utility` module retains description-
gate orchestration and compatibility re-exports, not duplicate vendor parsing.
HA still owns entity descriptions/IDs, executor and shutdown ownership, refresh,
credentials, image fetching/caching, config flows, translations, migrations and
media presentation. The 2.4.0 migration made no entity-registry changes.

## Fixes inherited with SDK 2.4.0

Microphone writes preserve unknown/speaker fields rather than inventing speaker
volume 100. Missing reported microphone configuration and non-finite writes fail
before IO. Port updates preserve enclosing enable flags and extension fields in
fresh/cached configurations; write objects remain detached. JPEG extraction
keeps embedded EXIF thumbnails inside their containing frame. The SDK closes
source/cropped images and does not change Pillow's process-global truncated-
image policy; undecodable composites pass through. No live device outcome is
inferred from offline regression tests.

Cloud-image records are bounded before AES decryption; opaque trailers are not
ciphertext. Clear multi-image selection no longer needs a password. JPEG-only
splitting/cropping preserves recognized non-JPEG bytes without transcoding.
The app's local PNG encoding does not establish PNG cloud responses. Existing
HA worker execution, lens indices and clear-only caching remain HA-owned; no
new key modes or credential UI were exposed by the 2.4.0 migration.

## Historical dependency qualification

The SDK 2.4.0 migration was qualified on HA 2026.10.4, HA 2026.11.0 and Core-dev.
The flow and diagnostics checks reported 100% statement and branch coverage.
Actual HA number-service controls covered field-preserving microphone writes,
observed state, owned worker execution and failures.

To run checks against the current pin, follow the isolated profiles in
[Development and checks](development.md), including the
[scoped Core checks](core-development-checks.md). Do not set `PYTHONPATH` to an SDK
checkout: the shared runner checks the installed SDK distribution version against
the manifest and profile before running tests.

## Narrow Core dependency conflict

Core's optional built-in EZVIZ integration still requests `pyezvizapi==1.0.5.0`.
`tools/check_hassfest.py` permits only the exact custom-only
`pyezvizapi>=2.6.0` conflict with that pin, with a visible warning. Other packages,
changed versions, additional errors and Core integrations remain fatal; negative
controls cover the old 2.2.0 and 2.3.0 requirements too. This is **not** stock hassfest
success or proof the built-in and custom integrations can coexist. Do not enable
them together. Remove the exception when upstream alignment makes it unused.
