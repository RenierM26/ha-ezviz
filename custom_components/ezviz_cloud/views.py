"""Ezviz Integration views (proxy and decrypt images)."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
import binascii
from http import HTTPStatus
import logging

from aiohttp import ClientError, ClientTimeout, web
from pyezvizapi.constants import HIK_ENCRYPTION_HEADER
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.utils import decrypt_image

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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


@callback
def _encryption_key(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> str | None:
    """Prefer a usable runtime key, otherwise the private camera options."""
    return (entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {}).get(serial, {}).get(CONF_ENC_KEY)


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

        runtime = getattr(entry, "runtime_data", None)
        coordinator = runtime.coordinator if runtime is not None else None
        if coordinator is None or coordinator.shutting_down:
            return web.Response(
                text="EZVIZ integration is unavailable", status=HTTPStatus.SERVICE_UNAVAILABLE
            )

        enc_key = _encryption_key(self.hass, entry, serial)

        # Security: never forward HA's incoming request headers (e.g., Authorization)
        # to third-party endpoints. Build a minimal, safe header set.
        headers = {
            "User-Agent": "HomeAssistant/ezviz_cloud",
            "Accept": "*/*",
        }

        resp = None
        try:
            resp = await self.session.get(
                raw_url,
                headers=headers,
                timeout=ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=20),
            )
            if resp.status != HTTPStatus.OK:
                return web.Response(text="Unable to fetch image", status=resp.status)
            content_type = resp.content_type or "image/jpeg"
            body = await resp.read()
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Error fetching EZVIZ image (%s)", type(err).__name__)
            return web.Response(text="Unable to fetch image", status=HTTPStatus.BAD_REQUEST)
        finally:
            if resp is not None:
                resp.release()

        # Try to decrypt if key available; the helper returns original if unencrypted
        if enc_key:
            try:
                body = await coordinator.async_execute(decrypt_image, body, enc_key)
            except HomeAssistantError:
                return web.Response(
                    text="EZVIZ integration is unavailable", status=HTTPStatus.SERVICE_UNAVAILABLE
                )
            except PyEzvizError as err:
                _LOGGER.debug("Image decryption failed (%s)", type(err).__name__)
                return web.Response(text="Unable to decrypt image", status=HTTPStatus.BAD_REQUEST)
        # If no key is set and the payload looks encrypted, warn once per request
        elif body[: len(HIK_ENCRYPTION_HEADER)] == HIK_ENCRYPTION_HEADER:
            _LOGGER.warning(
                "Image appears encrypted but no encryption key is set for camera %s",
                serial,
            )

        return web.Response(body=body, content_type=content_type)
