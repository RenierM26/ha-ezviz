***Name changed to Ezviz Cloud. (hassio domain : ezviz_cloud). Fixes a few weird issues with hassio, and allowed me to submit an official pull request.***

ezviz component for HASSIO, based on my fork of pyezviz

Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) **Rewrote integration based on latest framework. Please install via hassio integration after copying files to the "custom_components" folder.
3) Add camera username/password under ezviz_cloud domain. Please see example below. (Legacy and not required)

****Legacy, you can now configure all settings via the integration. Just add another ezviz integration to add camera rtsp credentials******

From 0.0.4.0 onwards:

```yaml
ffmpeg:
stream:
camera:
  - platform: ezviz_cloud
    username: "Ezviz account username"
    password: "Ezviz account password"
    cameras:
      D666321311:
        username: admin
        password: Password_from_camera_sticker
      D666321312:
        username: admin
        password: Password_from_camera_sticker
      E666321313:
        username: admin
        password: Password_from_camera_sticker

```

4) RESTART Home Assistant
5) Go to Configuration > Integrations, add and find the "Ezviz" integration. (If you did not have yaml config entries. If you did, it will import automatically and create integrations for you).

Only required if did not have yaml entries:

6) Type in your ezviz account username and password. Also please check your region code.
7) Your camera(s) should now be present on Hass

Notes:

1) Please use your main account. It doesn't seem to be working with shared accounts at the moment.
2) Hassio makes use of rtsp for camera streaming. This function will only work on the local network. (Mabe we'll be able to reverse engineer the ezviz cloud rtsp proxy in the future)
3) If your ezviz account is in a region other than EU, you'll need to do all the config via the integration. (Due to region code not imported from config)
