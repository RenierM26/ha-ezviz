# SDK feature boundary migration

This integration requires **published pyezvizapi 2.3.0 or newer**, with CI and
local check profiles pinned to exactly 2.3.0 from PyPI. The manifest and
`requirements/sdk.txt` agree. The release contains both the extraction
[SDK #438](https://github.com/RenierM26/pyEzvizApi/pull/438) and the focused-module
refactor [SDK #439](https://github.com/RenierM26/pyEzvizApi/pull/439).
No editable checkout, source override or direct-Git dependency is required.
Pillow is already an explicit Home Assistant core dependency, so the SDK image
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
media presentation. This migration makes no entity-registry changes.

## Intentional inherited fixes

Microphone writes preserve unknown/speaker fields rather than inventing speaker
volume 100. Missing reported microphone configuration and non-finite writes fail
before IO. Port updates preserve enclosing enable flags and extension fields in
fresh/cached configurations; write objects remain detached. JPEG extraction
keeps embedded EXIF thumbnails inside their containing frame. The SDK closes
source/cropped images and does not change Pillow's process-global truncated-
image policy; undecodable composites pass through. No live device outcome is
inferred from offline regression tests.

## Published dependency qualification

Install normal isolated profile requirements and use the shared checks:

```
python -m pip install -r requirements/lint.txt
python tools/check.py lint
python tools/check.py standalone
# In separate native environments with tests/runtime/requirements.txt,
# requirements/sdk.txt and each supported Home Assistant version:
python tools/check.py native
# In the separate plugin-patched acceptance environment:
python tools/check.py acceptance
# In the scoped Core-dev environment:
python tools/check.py core --core /path/to/core --runtime-tests
```

Do not set `PYTHONPATH` to an SDK checkout. The shared runner checks the installed
SDK distribution version against the manifest/profile before running tests.
Both HA 2026.10.4/2026.11.0 and Core-dev are qualified; flow/diagnostics retain
100% statement/branch coverage. Actual HA number-service controls cover field-
preserving microphone writes, observed state, owned worker execution and failures.

## Narrow Core dependency conflict

Core's optional built-in EZVIZ integration still requests `pyezvizapi==1.0.5.0`.
`tools/check_hassfest.py` permits only the exact custom-only
`pyezvizapi>=2.3.0` conflict with that pin, with a visible warning. Other packages,
changed versions, additional errors and core integrations remain fatal; negative
controls cover the old 2.2.0 requirement too. This is **not** stock hassfest
success or proof the built-in and custom integrations can coexist. Do not enable
them together. Remove the exception when upstream alignment makes it unused.
