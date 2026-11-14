# SDK feature boundary migration (release-blocked draft)

This branch depends on the new public device controls, audio and image helpers in
[pyEzvizApi #438](https://github.com/RenierM26/pyEzvizApi/pull/438). **Do not merge or deploy it against SDK 2.2.0.**
The provisional next minimum is 2.3.0. Before landing, publish/qualify the actual
SDK release and update the manifest and `requirements/sdk.txt` together if its
version differs. The normal published-profile CI checks intentionally remain
unchanged: an unreleased candidate is not proof the published dependency works.

Moved to SDK:

- Chime/voice/microphone capability, range, decoding and field-preserving writes.
- Port-security read/modify/write, including SDK-owned cached fallback.
- `SupportExtView` matching and vendor feature/network/storage readers.
- Linked-tracking nested payloads and commands; encoded intelligent-app readers.
- Concatenated JPEG scanning and stacked-lens splitting/decryption.

HA retains entity descriptions/IDs, `passes_description_gates` orchestration,
executor ownership, coordinator refreshes, credential storage, image fetching and
caching, config flows, translations, migrations and media presentation. `utility`
re-exports SDK helpers so existing integration consumers have one implementation.
No entity-registry migration or live account/device access is part of this work.

Intentional fixes: microphone writes preserve unknown/speaker fields rather than
inventing a speaker volume of 100. Missing reported microphone configuration and
non-finite writes fail before I/O. SDK image processing does not alter Pillow's
process-global truncated-image flag; undecodable composites pass through.

## Candidate validation (not published-release qualification)

Use an isolated environment with the normal HA test requirements and Pillow.
Point `PYTHONPATH` at the companion SDK checkout and run explicit test layers:

```
PYTHONPATH=/path/to/companion-sdk python -m pytest tests/test_*.py
PYTHONPATH=/path/to/companion-sdk python -m pytest -p pytest_homeassistant_custom_component --asyncio-mode=auto tests/acceptance
PYTHONPATH=/path/to/companion-sdk PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin --asyncio-mode=auto -o asyncio_default_fixture_loop_scope=function tests/runtime
```

Run each layer in its own environment as documented in the runtime test guide.
After release, rerun the standard `tools/check.py` profiles against the published
wheel without a source override, including both supported HA versions/Core-dev.

The Core source Ruff/format/Pylint/mypy and translation checks pass locally.
The end-to-end policy runner currently stops at hassfest: its deliberately exact
2.2.0 custom-only dependency exception does not permit provisional 2.3.0.
Requalify/update that exact exception and its tests only when the released SDK
profile is known; do not widen it or claim stock Core dependency compatibility.
