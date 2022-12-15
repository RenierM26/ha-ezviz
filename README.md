[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![Donate](https://img.shields.io/badge/donate-Coffee-yellow.svg)](https://www.buymeacoffee.com/renierm)

Updated Ezviz HA component making use of latest version of pyEzviz (API behind the integration). (**Basically Beta version of ha component with up to date changes)

https://www.home-assistant.io/integrations/ezviz/

#### EZVIZ account setup (doesn't require IE):

1) Register your account here: https://i.ezvizlife.com/user/userAction!goRegister.action
2) Take note of your **User Name**, you will need it to the EZVIZ Integration setup
3) Login here: https://euauth.ezvizlife.com/signIn
4) Logged in access the user account here: https://i.ezvizlife.com/user/userAction!displayUserInfo.action (to add/manage cameras) or use the EZVIZ App


#### Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) RESTART Home Assistant

The integration makes use of Config flows for config. Please install and configure via hassio integration:

3) Go to Configuration > Integrations, add and find the "Ezviz(Beta)" integration. (If you did not have yaml config entries. If you did, it will import automatically and create integrations for you).
4) Type in your ezviz account username and password. Also please check your region code.
5) Your camera(s) should now be present on Hass


#### When upgrading from old verions:

**Legacy, you can now configure all settings via the integration page on hassio (also known as config flow).**

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


#### Notes:

1) Hassio makes use of rtsp for camera streaming. This function will only work on the local network. (Mabe we'll be able to reverse engineer the ezviz cloud rtsp proxy in the future)
2) Please disable encryption on your cameras.
3) MFA needs to be disabled or the integration setup will fail.
