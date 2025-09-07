[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Donate](https://img.shields.io/badge/donate-Coffee-yellow.svg)](https://www.buymeacoffee.com/renierm)

Updated Ezviz HA component making use of latest version of pyEzviz (API behind the integration). (**Basically Beta version of ha component with up to date changes)
https://www.home-assistant.io/integrations/ezviz/


# EZVIZ account setup (doesn't require IE):
---

1) Register your account here: https://i.ezvizlife.com/user/userAction!goRegister.action
2) Take note of your **User Name**, you will need it to the EZVIZ Integration setup
3) Login here: https://euauth.ezvizlife.com/signIn
4) Logged in access the user account here: https://i.ezvizlife.com/user/userAction!displayUserInfo.action (to add/manage cameras) or use the EZVIZ App

# Ezviz(Beta) — Configuration & Options

This integration logs into **EZVIZ Cloud**, subscribes to **MQTT** events, and lets you configure **per-camera RTSP** settings with a simple UI.

> **Heads up:** As of v4, all per-camera settings live under the cloud entry’s **Options**. Legacy per-camera entries are migrated automatically.

---

## Add the integration

1. **Settings → Devices & Services → Add Integration → _Ezviz(Beta)_**  
2. Sign in with your EZVIZ account.  
3. If EZVIZ requests a one-time 2FA, follow the prompt.  
4. On success, the cloud tokens are stored; entities and MQTT will load.

---

## Configure per-camera options

Open **Settings → Devices & Services → Ezviz(Beta) → Configure**, then select a camera to **Edit**.

You’ll see the following fields:

### Fields

- **Camera Username**  
  The local username used for RTSP on the device (often `admin`, or model-specific).

- **RTSP Path**  
  The path part of the RTSP URL.  
  **Default:** `/Streaming/Channels/102` (the **sub-stream**)  
  Common values:
  - `/Streaming/Channels/101` → main stream
  - `/Streaming/Channels/102` → sub-stream (lower bitrate; default)
  - NVRs typically follow the same pattern per channel.

- **Use Verification Code (VC) for RTSP** *(toggle)*  
  Switch RTSP authentication between:
  - **VC mode** (uses the **Verification Code**)  
  - **ENC mode** (uses the **Encryption Key**)

- **Verification Code**  
  The **sticker/verification code** printed on the camera/NVR. Present on all devices.

- **Encryption Key**  
  The device **Encryption Key** (used when encryption is **enabled** on the device).  
  > Encryption can be **disabled** on the device. If disabled, you can leave this as “fetch_my_key” or blank and use VC mode instead.

- **Validate credentials now** *(checkbox)*  
  One-time RTSP validation. When checked, the form will **test** the RTSP credentials before saving.  
  This does **not** store anything by itself; it only validates the values you entered.

> **Tip:** If you don’t know the VC or ENC, keep the default **`fetch_my_key`** value. The integration will fetch it from EZVIZ (you may be prompted for a one-time 2FA).

### What gets saved

- Per-camera settings are stored under the cloud entry’s **Options**.
- The **RTSP Path**, **auth mode** (VC vs ENC), and whichever secret you used (VC or ENC) are saved per camera.
- The **Validate** checkbox is **not** saved; it only runs a one-time check at submit time.

---

## How VC vs ENC works

- **Verification Code (VC)**  
  Always present (printed on a sticker). When **VC mode** is enabled, the RTSP password is the **verification code**.

- **Encryption Key (ENC)**  
  Only relevant if **Encryption** is **enabled** on the device. When **VC mode** is **off**, RTSP uses the **encryption key**.

- **Encryption disabled?**  
  Then ENC may be unnecessary; use **VC mode** for RTSP.

You can switch modes at any time with the toggle. After you save, entities reload to use the new settings.

---

## One-time 2FA during edit

When fetching VC or ENC from the cloud (because you left `fetch_my_key`), EZVIZ may require a **one-time 2FA**:

1. You’ll be prompted for a **Verification Code 2FA** (for VC fetch) or **Encryption Key 2FA** (for ENC fetch).
2. Enter the code you received; the integration fetches the value and returns to the edit form **prefilled** with the resolved secret.
3. Click **Submit** to save.

If validation fails (auth or connectivity), the form reopens with the **best-known values** so you can adjust and try again.

---

## Examples

**Typical main stream RTSP URL (VC mode):**
`rtsp://<username>:<verification_code>@<camera-ip>:554/Streaming/Channels/101`

**Typical sub-stream RTSP URL (ENC mode):**
`rtsp://<username>:<encryption_key>@<camera-ip>:554/Streaming/Channels/102`



> Only the **path** (`/Streaming/Channels/101` or `/Streaming/Channels/102`) is configured in Options; the integration composes the full URL for you.

---

## Troubleshooting

- **RTSP validation failed**
  - Check the **auth mode** (VC vs ENC) matches what the device expects.
  - Verify the **RTSP Path** (try `/Streaming/Channels/101` for main or `/Streaming/Channels/102` for sub).
  - Ensure Home Assistant can reach the camera’s IP: `rtsp://<camera-ip>:554`.

- **Keeps asking for 2FA**
  - Codes expire quickly; request a new code and enter it promptly.
  - Make sure you’re entering the code for the **action** you’re performing (VC fetch vs ENC fetch).

- **No events/updates**
  - Confirm the cloud login is still valid (reconfigure if needed).
  - Reboot the camera/NVR if RTSP/MQTT seems stuck.

---

## Migration (v4)

- Legacy per-camera config entries are merged into the cloud entry’s **Options**.
- Ignored legacy entries (`version < 4`) are cleaned up automatically.
- Entity identifiers are preserved.

---

