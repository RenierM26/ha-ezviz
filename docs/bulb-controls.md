# Draft bulb controls

This work extends the existing bulb light entity; its `<serial>_power` unique ID
and device association are unchanged. No additional configuration, credential,
SDK dependency or storage migration is introduced. It is a draft implementation
based on recorded LB1 data, not a physical-bulb qualification or a release.

## Controls and metadata

The SDK already supplies structured `featureItems`, product ID, brightness and
colour temperature. HA translates those capability descriptions into its native
light controls; it does not parse the original vendor `FEATURE` payload again.
The SDK's existing `set_device_feature_by_key()` sends every property update.

| Control | Required observed metadata | HA mapping |
| --- | --- | --- |
| On/off | Existing known power and product/item metadata | Existing `light_switch` boolean |
| Brightness | Writable numeric 1-100 range, known step, unscaled value | HA 1-255 via the standard brightness helpers; zero turns off |
| White temperature | Writable Kelvin range and enum `white` mode, with brightness | Native `color_temp_kelvin`; clamp/quantize to the reported range/step |
| RGB | Writable six-digit `#RRGGBB` value and enum `color` mode, with brightness | Native `rgb_color` tuple |

The recorded LB1 reports brightness 1-100 and 2700-6500 K with 10 K steps. The
implementation does not apply those limits blindly to another model: malformed,
read-only, unknown-scale/unit metadata does not gain those controls. Basic on/off
continues to work when the extra metadata is absent. No scene/music effect,
restart, reset, timer or schedule control is exposed from the remaining items.
Duplicate feature keys follow the SDK's first-match policy.

## State and writes

State remains entirely SDK-observed. Accepted target values are not displayed
optimistically. A white-mode poll supplies its Kelvin value; an RGB-mode poll
supplies its hex colour. With qualified colour controls, scene/music and
unrecognized active modes are reported as unknown, not invented white/RGB state. Missing or invalid readings remain
unknown rather than being clamped into valid observations.

A requested colour change explicitly selects `white` or `color`, then writes its
value, optional brightness, and `light_switch=true`. Brightness alone does not
change colour mode. A plain off writes only `light_switch=false`. These are
separate SDK/HTTP commands, not an atomic device transaction. The account's
existing owned write lock prevents another control sequence interleaving them.
Every request is validated against current metadata after acquiring that lock.

The first failed property stops later commands. Earlier properties may already
have changed: no rollback, blind write retry or all-or-nothing claim is made.
Success and failures request the existing coalesced observed refresh, including
when the waiting caller is canceled. Device/account availability still gates IO.
The operation and SDK worker remain owned through cancellation and unload.

## Qualification boundary

The source and sanitization are documented in
[fixture provenance](../tests/fixtures/README.md). Tests exercise the real
published SDK encoder, native HA light services/state, range conversion, mode
changes, duplicate/missing metadata, queueing and partial/canceled sequences with
network access denied. The original SDK inventory fixture remains covered.

Mode-first ordering and behavior while physically off still require device
confirmation. These tests cannot establish that a bulb accepts every property,
that a partial update is harmless, or that a particular firmware implements the
advertised capability correctly. Keep this PR draft until that qualification is
reviewed. No production bulb commands were issued while preparing the draft.
