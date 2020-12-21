# ha-ezviz
ezviz component for HASSIO, based on my fork of pyezviz

Install:

1) Copy to custom_components folder or use HACS custom repository. (Custom repository : RenierM26/ha-ezviz)
2) Setup configu using instruction from https://www.home-assistant.io/integrations/ezviz/. NOTE: You'll need to rename the integration as follows:

camera:
  - platform: ha-ezviz     #<----------This part changes
  
