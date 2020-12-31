# ha-ezviz
ezviz component for HASSIO, based on my fork of pyezviz

Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) Setup configu using instruction from https://www.home-assistant.io/integrations/ezviz/. NOTE: You'll need to rename the integration as follows:

Example configuration.yaml with deviation from official broken integration.

stream:
ffmpeg:
camera:
  - platform: ha-ezviz						<----Platform name.
    username: !secret ezviz_username
    password: !secret ezviz_password
    region: eu								<----Region code (Optional, defaults to EU).
    cameras:
     D666666666:
      username: admin
      password: STUABC
  
