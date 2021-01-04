***Name changed to Ezviz Cloud. (hassio domain : ezviz_cloud). Fixes a few weird issues with hassio, and allowed me to submit an official pull request.***

ezviz component for HASSIO, based on my fork of pyezviz

Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) **Rewrote integration based on latest framework. Please install via hassio integration after copying your config to the "custom_components" folder.
3) Add camera username/password under ezviz_cloud domain. Please see example below.

```yaml
stream:
ffmpeg:
camera:

ezviz_cloud:
  cameras:
    D6666660:
      username: admin
      password: Password_from_camera_sticker.
    D666678:
      username: admin
      password: Password_from_camera_sticker.
    D66666648:
      username: admin
      password: Password_from_camera_sticker.
```
