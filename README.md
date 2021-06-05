[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![Donate](https://img.shields.io/badge/donate-Coffee-yellow.svg)](https://www.buymeacoffee.com/renierm)

Updated Ezviz HA component making use of latest version of pyEzviz (API behind the integration). (**Basically Beta version of ha component with up to date changes)

https://www.home-assistant.io/integrations/ezviz/

Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) RESTART Home Assistant
3) **Rewrote integration based on latest framework, the integration makes use of Config flows for config. Please install and configure via hassio integration.
4) Go to Configuration > Integrations, add and find the "Ezviz(Beta)" integration. (If you did not have yaml config entries. If you did, it will import automatically and create integrations for you).
5) Type in your ezviz account username and password. Also please check your region code.
6) Your camera(s) should now be present on Hass


When upgrading from old verions:

****Legacy, you can now configure all settings via the integration page on hassio (also known as config flow).******

From 0.0.4.0 (Legacy Yaml method no longer needed on latest version!):

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





Notes:

1) Please use your main account. It doesn't seem to be working with shared accounts at the moment.
2) Hassio makes use of rtsp for camera streaming. This function will only work on the local network. (Mabe we'll be able to reverse engineer the ezviz cloud rtsp proxy in the future)
3) If your ezviz account is in a region other than EU, you'll need to do all the config via the integration. (Due to region code not imported from config)
