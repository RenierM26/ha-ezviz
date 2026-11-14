# Historical migration fixtures

All credentials, serials, names and account addresses here are synthetic. These
are schema fixtures, not exports from a user's `.storage` directory.

| Fixture | Historical source | Contract |
| --- | --- | --- |
| `v1.json` | Parent of `74f44d5` (#153), `config_flow.py` VERSION 1 | Per-camera password; no independent encryption key or RTSP selector. Cloud token lacks internal `user_id`. |
| `v2.json` | `74f44d5` (#153), VERSION 2 | Independent `enc_key`; no RTSP selector. |
| `v3.json` | Parent of `8e78d6d` (#188), VERSION 3 | Explicit encryption/verification-code RTSP choice, per-camera stream path and separate cloud account. Post-#183 cloud has `user_id`. |
| `v4.json` | `8e78d6d` (#188), VERSION 4 | Camera credentials consolidated under cloud `options.cameras`; version-5 migration changes authentication storage only. |

Expected credential transformations come from the *historical migration code*,
not today's constants: #153 copied v1 password to encryption key; #180
(`6797dc8`) selected verification-code RTSP for v1/v2. The final pre-#188
migration also filled an empty encryption key from the password. This is why a
v1/v2 direct upgrade must not simply apply the v3 default `False` selector.
Old cloud tokens without an internal SDK user ID require reauthentication; the
login email must not be fabricated into an internal ID.

Legacy entity unique-ID cases follow parent of `b64ebc5` (#189):
`<serial>_<camera name>.<description key>`. Real descriptor keys include
`Seconds_Last_Trigger`, `Motion_Trigger` and `PRIVACY`. Name punctuation is
literal, key-case variants are supported, names/serials are not guessed.

## Coverage and execution

`tests/runtime/test_migration.py` runs in **both native CI jobs**, HA 2026.10.4
and 2026.11.0, without the version-pinned HA test plugin. It uses actual config
entries, config-entry removal callbacks, device/entity registries, Repairs and
HA's scheduled Store saves (including stop/reload from disk). Only initial test
registration uses the same internal entry index as HA's own test helper. No
production credentials, network, camera actions or manual `.storage` writes.

Tests cover v1–v4, latest/future versions, idempotency, existing options winning,
defaults, missing serials, ignored/current/foreign entries, ambiguous accounts,
removal failure/cancellation and retry, real storage restart, unchanged user
entity IDs/settings, exact-match UID upgrades, case/rename/presence filtering,
collisions, unavailable/renamed cameras and bounded/cleared Repairs issues.
The target schema is now version 5: minimal cloud routing data and one private SDK
snapshot. `test_token_layout.py` additionally covers the later v4 embedded-SDK/anchor
envelope, latest-token precedence, failed fsync/metadata commits, stale-owner rejection
and initial-flow private staging. These use synthetic API-shaped snapshots, not a
historical user export. Legacy accounts missing internal SDK IDs still require reauth.

This follows HA's [config-entry lifecycle](https://developers.home-assistant.io/docs/config_entries_index/)
and [entity-registry migration API](https://developers.home-assistant.io/docs/entity_registry_index/):
copy data/options, update entries through `async_update_entry`, migrate unique
IDs through the registry API, preserve entity IDs and reject future major
versions. Current custom-integration boolean callbacks remain compatible with
both supported HA runtimes. Orderly shutdown/restart persistence is tested;
arbitrary process death during a multi-entry removal is not an atomic database
transaction.

Run from the repository root in either native environment:

```sh
python -m pytest --asyncio-mode=auto \
  -o asyncio_default_fixture_loop_scope=function tests/runtime
```
