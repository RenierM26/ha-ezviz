# Native Home Assistant tests

These tests run on the minimum supported HA 2026.10.4 and current HA 2026.11.0,
with the published minimum SDK, without overriding HA's dependencies or applying
version-pinned test-plugin patches. `conftest.py` provides isolated real HA core,
config entries, registries and storage, and prohibits network connections and DNS.

## Config flow

`test_config_flow.py` replaces the old core-checkout-only
`tests/components/ezviz_cloud/test_config_flow.py` suite (which mocked the obsolete
`login()` boundary). It uses real config/options flow managers and schema validation,
patching only SDK/RTSP IO and integration setup/reload. SDK tokens and camera values
are synthetic fixtures; no real credentials or device requests are needed.

Coverage includes setup with EU/RU/custom hosts, duplicate accounts, all login and
MFA failures/retries, context-bound reauthentication, legacy token recovery,
account-change rejection, cloud options, camera selection, category-based defaults,
manual/fetched credentials, encryption/sticker MFA, partial-result prefills, RTSP
verification/encryption auth, failure recovery, and non-persistence of ephemeral
codes/flags. One explicitly documented direct handler test checks the defensive
no-pending-MFA guard, which cannot be reached through a normal menu.

CI requires **100% statement and branch coverage** of `config_flow.py` on both HA
versions. Defensive exception handlers are included, not marked `no cover`.

```sh
python -m pip install -r tests/runtime/requirements.txt 'homeassistant==2026.11.0' 'pyezvizapi==2.0.0'
python -m coverage run --branch --source=custom_components.ezviz_cloud.config_flow -m pytest --asyncio-mode=auto -o asyncio_default_fixture_loop_scope=function tests/runtime
python -m coverage report --show-missing --fail-under=100
```

Guidance checked against current Home Assistant developer documentation:

- https://developers.home-assistant.io/docs/development_testing/
- https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#reauthentication
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow-test-coverage/

Migration fixture provenance is documented in `fixtures/migration/README.md`.
