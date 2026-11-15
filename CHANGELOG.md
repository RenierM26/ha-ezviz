# Change history

## Unreleased — main

Not a published integration version. Manifest `0.2.0.25` is still the older
published version number; these later changes require a new version/tag before
release. Historical published notes remain on [GitHub Releases](https://github.com/RenierM26/ha-ezviz/releases).

### Upgrade considerations

- The legacy URL-valued `last_alarm_pic` diagnostic sensor is removed, including
  its account-scoped current/historical registry rows during sensor setup. Update
  consumers to the existing Last motion image entity; image/media source URLs
  stay internal. No automatic consumer rewrite or history/recorder purge occurs.
  This fixes [#222](https://github.com/RenierM26/ha-ezviz/issues/222) without moving
  signed/private URLs to another public state/attribute.

- Requires HA 2026.10.4+, Python 3.14.2+ and published SDK 2.4.0+.
  Built-in/custom EZVIZ coexistence has an unresolved SDK-pin conflict.
- Encryption-key text entities are write-only. IDs and `text.set_value` remain;
  state is empty, not restored, and cannot supply an automation's secret. Existing
  recorder/history/backups are not purged. Use camera options for configuration.
- Version-5 account metadata moves the complete SDK token into one private durable
  snapshot. Preserve the HA config volume and a coherent pre-upgrade backup;
  source-only downgrade is not promised to reverse migrations.

### Integration and reliability

- Add an opt-in per-camera **RTSP service** configuration switch using the
  published SDK's local command-port API (TCP 8000). Setup is read-only and
  excludes battery families. A three-choice Local device controls dropdown in
  Camera settings reuses the existing username and chosen verification/sticker
  code or encryption key, without another stored credential or fallback. The
  shared local backend is reusable by future controls; the separate playback
  credential choice is unchanged, and its one-time test now defaults off.
  Local status is polled every five minutes, independently of cloud health.
  Writes preserve unrelated services and publish only verified read-back; no
  automatic port opening, credential fallback, watchdog or write retry.


- Coalesce ordinary cloud-setting refreshes about two seconds after the latest
  accepted command. Camera switches/lights, numbers/selects, plug/bulb power,
  camera name and motion detection display only SDK-observed state; the old value
  may remain until the cloud catches up. No optimistic overlay, confirmation
  matching, custom retries or display-expiry timers. Serialized, owned writes use
  detached SDK input and short-lived command-only sibling context to prevent rapid
  compound settings from overwriting each other. Availability remains authoritative;
  no getter waits, snapshot mutation, secret retention or siren/alarm/action change.
  Two seconds is an initial propagation allowance, not a universal guarantee.

- Coalesce concurrent cold reads per image generation and share in-flight image
  downloads across lenses. Viewer cancellation does not cancel other viewers;
  stale URL/key/capture results stay uncached and shutdown owns the work.
  SDK decoding remains per lens (no new SDK batch API or vendor parsing in HA).
- Skip no-image/identical push updates and discovery on unchanged inventory;
  detach only the affected camera row, preserving poll health and public events.
- Materialize SDK inventory once off-loop per poll, with a detached camera
  projection so SDK write adapters cannot mutate accepted inventory.

- Bind media-proxy requests to bounded, account/camera-owned image references;
  reject caller URLs and private DNS/IP targets. Follow at most five validated
  public HTTPS CDN redirects within one 30-second download deadline, keeping
  connection-time DNS checks on every hop. Reject downgrades, unsafe/malformed
  targets and missing locations. Validate yarl's transport-normalized hostname
  before public-IP/numeric checks, including Unicode-dot/fullwidth IP forms;
  keep the original signed request string. Old proxy links require browsing again.
- Decode media-browser images through the SDK even without a key (first lens),
  reject unresolved encryption and serve the decoded image content type.
- Publish siren availability on coordinator failure/recovery without polling or
  changing local assumed state/timer semantics.
- Camera switches/lights now await shared authoritative refresh rather than
  publishing optimistic values that unrelated alarm pushes could roll back.
  Light turn-on completes brightness/power writes before one observation refresh;
  accepted brightness is still reconciled if the later power write fails.

- Recognize clear image signatures before rejecting embedded encryption markers,
  including legal JPEG metadata in fresh/cached Last motion and Snapshot images.
  Keep residual ciphertext rejection and recovery. Image fetch failures no longer
  include private cloud URLs or raw exception messages in integration error logs.


- Require published SDK 2.4.0 for length-delimited cloud image decryption and
  clear multi-image selection. CI qualifies the exact PyPI artifact. JPEG-only
  splitting/cropping preserves non-JPEG bytes without PNG transcoding.

- Move vendor audio, capability/metadata, port-security, linked-tracking/app and
  multi-lens image handling to published SDK 2.4.0. Keep HA entity IDs,
  descriptions, executor/refresh/shutdown ownership, fetching/caching and config
  lifecycles unchanged. Microphone writes now preserve speaker/unknown sibling
  fields rather than defaulting speaker volume to 100; missing configuration
  and non-finite writes fail before IO. Port writes preserve enclosing enable
  flags and cached vendor fields; JPEG parsing preserves embedded thumbnails.
  Image decoding no longer changes Pillow's process-global truncated-image flag;
  undecodable composites pass through. See [the migration](docs/sdk-feature-boundary.md).

- Do not return or cache encrypted bytes as JPEGs when cloud image decoding fails
  or no usable image key is available. Discard previously encrypted cache entries
  and retry on the next request; retain valid-image caching and worker/shutdown
  ownership. Decoder errors no longer incorrectly identify a key mismatch.

- Gate the existing chime/Alarm volume number on enabled `SupportDeviceRing` (185)
  plus valid observed metadata, including write-time checks and the app-backed
  0–7 DBScreen scale guard instead of a universal 0–100 range. Published SDK 2.2.0
  still filters DBScreen from camera inventory; this is not new discovery support.
- Add a separate **Alarm voice volume** number for `SupportChangeVolume` (203 == 1)
  and observed `CustomVoice_Volume.volume`, with the app's 10–100 selectable range.
  Preserve microphone/unknown fields and report an observed muted zero without
  changing modes. Keep chime and voice settings independent; no ID/version/SDK
  minimum changes, full arm/disarm-profile restoration or live qualification.

- Add a per-camera **Alarm volume** configuration number (0–100) only when the
  camera reports a valid integer `ChimeMusic.volume`. Preserve the complete
  reported chime payload while changing its volume, using the normal owned
  executor/refresh/error path. This does not automatically restore an arm/disarm
  alarm profile or prove audible output. Adapted from
  [@blankcan](https://github.com/blankcan)'s original [PR #229](https://github.com/RenierM26/ha-ezviz/pull/229)
  and [device investigation #228](https://github.com/RenierM26/ha-ezviz/issues/228).

- Fix options-save failures with loaded cameras by removing their redundant
  config-entry listener; HA owns options reloads, while credential actions retain
  coordinator-driven stream updates.

- Shared typed SDK inventory/freshness for cameras, detectors, bulbs and plugs;
  retain IDs and stale-row health instead of inventing online/capability semantics.
- Optional channel-99 push independent of polling, stable persisted installation
  identity, durable token acknowledgement and retained shutdown/credential ownership.
- Camera capture/snapshot handling, timer/stream registry-rename lifetime fixes,
  scoped push device lookup, safe translated HTTP/API error categories and published
  SDK primary-error preservation through switch fallback.
- Cached allowlisted diagnostics, native HA producer/restoration/storage controls,
  strict Core policy and isolated test layers. Detector binary semantics and live
  device/push/browser qualification remain explicitly limited.

### Repository maintenance

- Structured issue forms, contributor/support/security guides, path labels and
  grouped dependency PRs. Always-running merge gate handles docs-only changes;
  manually dispatched release preparation qualifies exact source and creates
  drafts only, without tagging, publishing or deploying.

- Shared development/editor/CI checks; audited ownership and modular flow/state
  boundaries; behavioral recovery tests.
- Current setup, support, migration and release guides; immutable validation pins,
  bare-version tag validation and a read-only source/metadata release preflight.

See the [overhaul tracker](docs/overhaul-backlog.md) for merged/in-review status.
