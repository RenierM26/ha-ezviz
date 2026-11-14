# Alarm volume

For the **unreleased development line**, not a newly published HACS update.
See [release status](../README.md#release-status).

Cameras that report a valid integer `ChimeMusic.volume` from 0 to 100 gain an
**Alarm volume** number under their device's **Configuration** controls. Zero is
a valid value, not a reason to hide the entity. The unique-ID suffix is
`_alarm_volume`; no existing entities are renamed.

Changing it writes only the `volume` member of the last reported `ChimeMusic`
object, preserving the other chime settings (including fields this integration
does not recognize). It uses the camera's reported channel and the normal owned
SDK executor, then requests a cloud refresh. The displayed value follows the
reported state rather than optimistically confirming a write. A failed command
raises the usual translated error; missing/malformed current chime data rejects
a write instead of inventing a default payload.

This control is separate from **Microphone volume** and from the alarm sound-mode
selector. It does not update `CustomVoice_Volume`, call a whistle/siren action,
change defence mode, or automatically apply the EZVIZ app's complete arm/disarm
profile. In particular, adding this setting alone is not a promise that arming
restores a previously muted alarm. Avoid writing a guessed volume to devices
that do not report this capability.

No physical device action is required for the offline tests. SDK acceptance,
a cloud-reported slider value and audible alarm output are different outcomes;
live behavior on a specific model/firmware still needs separate verification.

## Contribution credit

This feature adapts [@blankcan](https://github.com/blankcan)'s
[original PR #229](https://github.com/RenierM26/ha-ezviz/pull/229), including its
payload-preservation approach and regression case, to the current HA/SDK and test
architecture. Their [investigation in #228](https://github.com/RenierM26/ha-ezviz/issues/228)
identified and tested the writable `ChimeMusic.volume` setting on their camera.
That historical observation is not a new live-device qualification by this PR.
