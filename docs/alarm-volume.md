# Chime and alarm voice volume

For the **unreleased development line**, not a newly published HACS update.
See [release status](../README.md#release-status).

These are independent settings under a device's **Configuration** controls:

| HA control | Required capability and observed payload | Selectable range |
| --- | --- | --- |
| **Alarm volume** (existing ID suffix `_alarm_volume`) | Enabled positive integer `SupportDeviceRing` / `supportExt["185"]` and valid `ChimeMusic.volume` | 0–100, or 0–7 for `DBScreen` data |
| **Alarm voice volume** (new suffix `_alarm_voice_volume`) | `SupportChangeVolume` / `supportExt["203"] == 1` and valid `CustomVoice_Volume.volume` | 10–100 |

Numeric strings are accepted for the capability flags, not booleans, floats,
empty/disabled values, malformed tokens or mere key presence. Volume readings
must be actual integers within the relevant scale. An observed voice volume of
**zero** remains a valid muted reading even though the app's voice slider selects
10–100. Other voice values below 10 are not assumed to be supported. No default
volume is invented when metadata is missing or malformed.

Changing either number modifies only `volume` in its complete **last reported**
object, preserving microphone gain and other/unknown fields. It never changes the
other chime object. The command uses the reported channel and normal owned SDK
executor, then requests a cloud refresh. Displayed values remain observed, not
optimistic confirmations. The capability and current payload are checked again
before each write, so an existing entity cannot bypass a subsequently disabled
flag. Command errors use the integration's translated error handling.

Neither number changes alarm sound mode, defence mode or microphone gain; neither
triggers an audition/whistle/siren action or applies the EZVIZ app's complete
arm/disarm profile. A zero voice reading is not an instruction to change modes.
HA can configure saved voice volume independently; the app UI's temporary slider
disabling in silent mode is not treated as loss of the hardware capability.
Permissions are enforced by EZVIZ: capability presence does not prove that the
current account may perform the action. No account role is inferred from errors.

## Compatibility and limits

No existing entity ID is renamed and no registry entry is deleted. Previously
exposed chime numbers without enabled capability 185 are no longer created;
existing registry rows can remain unavailable after reload. Update automations
that assumed a universal 0–100 chime range: values above 7 are rejected by HA for
`DBScreen` data, and voice-volume requests below 10 are rejected.

The **published SDK 2.2.0 currently filters `DBScreen` from its accepted camera
inventory**. Its 0–7 guard is defensive when such data reaches HA; this change does
not add SDK discovery/support for that category. Native tests distinguish the
actual SDK producer boundary from synthetic accepted-data/platform controls.

All tests are offline. SDK acceptance, a cloud-reported value and audible output
are different outcomes; no live model/firmware or physical alarm qualification
is claimed. No release, deployment or SDK-minimum change is implied.

## App evidence

The gate/range audit used retained **EZVIZ Android 7.4.1.0421**, versionCode 1676,
not a claim about the latest app. Generated app code is not shipped in this repo.

- Base APK SHA-256: `90d1007934e7878f44b3e1f94b65ccd2d0a29f967b23882cd54c9e5e815fd663`.
- Asset split SHA-256: `31441b708d1620f2241aad68d1689f6a730b603f0e803ad050d35400a90a543f`.
- `RevisionSetting/pages/Audio/audioSettingViewModel.js` gates chime pages on
  enabled 185. `DBScreen` routes to `screenSoundSetting.js` (0–7). The other
  `chimeVolumeSetting.js` and `chimeSoungSetting.js` sliders use 0–100; 550/735
  select between pages, not universal volume prerequisites.
- `DeviceDefenceWarningToneActivity.getData()` uses 203 == 1 for voice volume;
  `configVolume()` / `DeviceRemoteDataSource.setCustomVoiceVolume()` write
  `CustomVoice_Volume`, not `ChimeMusic`.
- RN `deviceVoiceReminder.js` / `decoupledVoiceAlarm.js` also use 203, expose a
  10–100 voice slider and preserve the other fields in the reported voice object.
  Native shared-device hiding is not generalized into an unverified ban on every
  RN path. Malformed JS truthiness is not emulated for capability checks.

## Contribution credit

The chime control adapts [@blankcan](https://github.com/blankcan)'s
[original PR #229](https://github.com/RenierM26/ha-ezviz/pull/229), including its
payload-preservation approach and regression case, to the current HA/SDK and test
architecture. Their [investigation in #228](https://github.com/RenierM26/ha-ezviz/issues/228)
identified and tested writable `ChimeMusic.volume` on their camera. That historical
observation is not a fresh live-device qualification of either control here.
