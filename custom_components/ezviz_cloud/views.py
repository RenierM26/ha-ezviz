"""Ezviz Integration views (proxy and decrypt images)."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import binascii
from http import HTTPStatus
import logging

from aiohttp import ClientError, ClientTimeout, web
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.utils import decrypt_image

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.text import DOMAIN as TEXT_PLATFORM
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ENC_KEY, DOMAIN, OPTIONS_KEY_CAMERAS

_LOGGER = logging.getLogger(__name__)


@callback
def async_generate_image_proxy_url(config_entry_id: str, serial: str, url: str) -> str:
    """Generate proxy URL for alarm image (decrypted if needed)."""
    return ImageProxyView.url.format(
        config_entry_id=config_entry_id,
        serial=serial,
        url=urlsafe_b64encode(url.encode("utf-8")).decode("utf-8"),
    )


class ImageProxyView(HomeAssistantView):
    """View to proxy and decrypt Ezviz alarm images."""

    requires_auth = True
    url = "/api/ezviz_cloud/image/{config_entry_id}/{serial}/{url}"
    name = "api:ezviz_cloud_image"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the proxy view."""
        self.hass = hass
        self.session = async_get_clientsession(hass)

    async def get(
        self, request: web.Request, config_entry_id: str, serial: str, url: str
    ) -> web.StreamResponse:
        """Return decrypted image bytes for an alarm picture."""
        try:
            raw_url = urlsafe_b64decode(url.encode("utf-8")).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return web.Response(
                text="Invalid encoded URL",
                status=HTTPStatus.BAD_REQUEST,
            )

        entry = self.hass.config_entries.async_get_entry(config_entry_id)
        if entry is None or entry.domain != DOMAIN:
            return web.Response(
                text=f"Unknown config entry id: {config_entry_id}",
                status=HTTPStatus.BAD_REQUEST,
            )

        # 1) Prefer runtime Text entity value (if enabled) for the enc key
        enc_key: str | None = None
        entity_reg = er.async_get(self.hass)
        text_entity_id = entity_reg.async_get_entity_id(
            TEXT_PLATFORM, DOMAIN, f"{serial}_camera_enc_key"
        )
        if text_entity_id:
            state = self.hass.states.get(text_entity_id)
            if state and state.state and state.state.lower() != "unavailable":
                enc_key = state.state

        # 2) Fallback to options mapping
        if not enc_key:
            enc_key = (
                (entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {})
                .get(serial, {})
                .get(CONF_ENC_KEY)
            )

        # Security: never forward HA's incoming request headers (e.g., Authorization)
        # to third-party endpoints. Build a minimal, safe header set.
        headers = {
            "User-Agent": "HomeAssistant/ezviz_cloud",
            "Accept": "*/*",
        }

        try:
            resp = await self.session.get(
                raw_url,
                headers=headers,
                timeout=ClientTimeout(connect=10, sock_connect=10, sock_read=20),
            )
        except ClientError as err:
            _LOGGER.debug("Error fetching Ezviz image: %s", err)
            return web.Response(text=str(err), status=HTTPStatus.BAD_REQUEST)

        if resp.status != HTTPStatus.OK:
            text = await resp.text()
            return web.Response(text=text, status=resp.status)

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        body = await resp.read()

        # Try to decrypt if key available; the helper returns original if unencrypted
        if enc_key:
            try:
                body = decrypt_image(body, enc_key)
            except PyEzvizError as err:
                # Invalid key or format; surface an error to caller
                _LOGGER.debug("Decrypt failed: %s", err)
                return web.Response(text=str(err), status=HTTPStatus.BAD_REQUEST)
        # If no key is set and the payload looks encrypted, warn once per request
        elif body[: len(HIK_ENCRYPTION_HEADER)] == HIK_ENCRYPTION_HEADER:
            _LOGGER.warning(
                "Image appears encrypted but no encryption key is set for camera %s",
                serial,
            )

        return web.Response(body=body, content_type=content_type)
