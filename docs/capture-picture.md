# Capture pictures and snapshot images

`ezviz_cloud.capture_picture` asks the cloud to take a new picture on channel 1.
It does not require RTSP; battery devices may wake for the request. The original
feature was contributed in [#235](https://github.com/RenierM26/ha-ezviz/pull/235).
Multi-lens capture selection is not added by this follow-up.

Example action (replace the entity ID):

```yaml
action: ezviz_cloud.capture_picture
target:
  entity_id: camera.example
response_variable: captured
```

The optional response contains an entity-ID-keyed `picture_url`. The corresponding
Snapshot image entity updates independently of the Last motion image entity.
The signed picture URL is held in memory, not in the image entity's state, and
is not restored after a restart. Treat URLs returned to automations as private.

## Download ordering and errors

Each capture advances an image generation, even when the server reuses a URL.
An older pending download is discarded if a newer capture arrives: it neither
returns stale bytes nor poisons the newer cache. Each image request performs at
most one download; the retired request returns no image, and the request for the
current capture can load its bytes. There is no automatic refetch loop.

Malformed capture results, missing/non-string URLs and non-HTTP(S)/hostless URLs
raise a controlled action error without publishing a snapshot update. Failed
image downloads are not cached. Existing JPEG loading/decryption and stable
entity/service identifiers are retained.

## SDK and validation boundary

This follow-up deliberately keeps `pyezvizapi>=1.0.5.0,<2.0.0` until the separate
inventory/channel-99 integration migration is ready. Both CI jobs exercise the
published 1.0.5.0 minimum; the bounded range passes stock hassfest without a
custom exception. Do not silently install SDK 2.0.0 into the unmigrated push path.

Real-HA acceptance uses Python 3.14 and HA 2026.10.4 with synthetic SDK responses
and mocked image downloads. It covers service dispatch/response, snapshot cache
ordering, plain JPEG handling and error cases; it does not perform real camera
captures or claim live device qualification.

```sh
python -m pip install -r tests/acceptance/requirements.txt pyezvizapi==1.0.5.0
python -m pytest -p pytest_homeassistant_custom_component --asyncio-mode=auto tests/acceptance
```
