# Recorded device fixtures

## Issue #173: LB1 bulb

Source: [issue #173](https://github.com/RenierM26/ha-ezviz/issues/173),
[device_infos.json](https://github.com/user-attachments/files/23508744/device_infos.json)
and [diagnostics_export.json](https://github.com/user-attachments/files/23508714/diagnostics_export.json).

The device-info attachment is a UTF-16/BOM SDK export, not the original HTTP
pagelist response. The diagnostic field named `ezviz_api_page_list` also contains
an exported per-device mapping. `issue_173_lb1_pagelist.json` reconstructs a minimal
pagelist-shaped response so tests exercise the current SDK's real
`get_device_infos()` → record-building → `poll_devices()` pipeline, then HA.

Only the LB1 is retained. Device/account identifiers, product ID and name are
synthetic; authentication, routing, addresses, MACs, cloud/media URLs, timestamps,
schedules and other cameras are excluded. The five control feature-item objects
(power, brightness, white/RGB mode, colour temperature and RGB colour) preserve
source values, types and capability descriptions. `supportExt`, model category
and firmware version are retained. Added empty/default sections are structural,
not an additional recorded response. This is deliberately not a full raw dump.

The original unmodified export was also replayed privately through published
SDK 2.4.0: one `LightState` plus two cameras; the LB1 is online/on with brightness
100 and colour temperature 6500 K. The sanitized fixture retains those results.
Tests make no live requests. They prove parsing, discovery and command encoding,
not physical bulb behaviour or additional supported HA controls.
