# Upgrading, credential storage and rollback

This describes the **unreleased main development line**, not a new published
release. Read the target tag's release notes and [release status](../README.md#release-status)
before installing. A PR merge does not publish a HACS update.

## Before upgrading

1. Record your installed tag/commit, HA version, enabled custom/built-in EZVIZ
   entries, important entity IDs and automations that use encryption-key text state.
2. Keep a restorable HA/configuration backup from **before** the upgrade, including
   hidden private storage, integration files and config-entry options. Treat it
   as private; a component source ZIP alone does not contain credentials/state.
3. Verify the target's HA baseline and SDK requirements. Main requires HA2026.10.4+
   and SDK2.2.0+; built-in EZVIZ's conflicting SDK pin is not qualified alongside it.
4. Upgrade through your chosen installation method and follow its restart prompt.
   Keep the same HA config volume. Confirm polling, push, entity identities and
   representative functions separately; do not assume cloud or physical success
   from code installation alone.

## Existing entry migration

Historical per-camera entries/settings are consolidated into cloud-entry options.
Current account metadata is version 5; old token layouts are imported into a
single private SDK snapshot before public credential copies are removed. Stable
entry/device/entity identities and existing options are preserved by the qualified
migration paths. Failed writes keep the prior data/reference for retry rather
than acknowledging an uncommitted migration.

Existing private snapshots preserve opaque SDK fields, host identity and push
replay state. A missing snapshot asks for reauthentication; it does not silently
fall back to duplicate stale config-entry tokens. A fresh login uses the same
persisted account identity. Expired/revoked credentials can still require reauth.
Do not hand-edit/delete `.storage`, copy tokens from another active deployment or
change a container MAC to try to repair account auth.

## Breaking change: encryption-key text state is write-only

Text entities with the stable unique-ID suffix `_camera_enc_key` retain their
entity IDs and `text.set_value` action, but always report an empty state and do
not restore the previous secret. Read-only clients must obtain configuration
through appropriate internal credential ownership, not a text entity's state.

Update automations/templates/scripts that read that old state. Configure camera
credentials through **Options → Camera Settings**. An empty text state is expected
and does not mean the saved key was lost. Writing a new key is a device operation,
not a harmless way to query it. This change does **not** purge existing recorder,
history, logs or backups containing earlier values.

## Recovery and rollback

- For a recovered vendor timeout, leave the working installation alone; use
  [health/permission troubleshooting](troubleshooting.md) first.
- For actual missing/expired credentials, use HA's same-account reauth prompt.
  After fixing storage permissions/free space, reload as appropriate; do not
  delete the private snapshot as a routine reset.
- Reinstalling an old component version does **not** reverse config-entry/token
  migrations. Older code may reject version-5 entries or misunderstand storage.
  There is no promise of source-only downgrade compatibility.
- A coherent rollback may require the **matching pre-upgrade backup**, including
  integration code and relevant configuration/private state. A full HA restore
  also rolls back unrelated changes and restarts HA; choose that scope deliberately.
  Do not combine arbitrary old token files with newer replay/account state.
- Do not run the live instance and a restored clone simultaneously with the same
  token/replay state. Do not delete/re-add the integration casually: that can
  change identities and require credentials/permission work again.

See [the detailed storage/migration contract](shared-inventory.md#channel-99-and-credentials)
and [lifecycle limits](lifecycle-audit.md) for technical boundaries.
