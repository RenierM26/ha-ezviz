"""Ezviz Integration views (proxy and decrypt images)."""

from http import HTTPStatus
from ipaddress import ip_address
import logging
import socket
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.resolver import ThreadedResolver
from pyezvizapi.devices.images import process_image_bytes
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.image import infer_image_type
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_ENC_KEY, DOMAIN, OPTIONS_KEY_CAMERAS
from .image import _has_residual_encryption

_LOGGER = logging.getLogger(__name__)


class PublicImageResolver(ThreadedResolver):
    """Reject non-public DNS results at the actual connection boundary."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        """Use only globally routable addresses, including after DNS changes."""
        addresses = await super().resolve(host, port, family)
        if not addresses or any(
            not ip_address(item["host"]).is_global for item in addresses
        ):
            raise OSError("Non-public image target")
        return addresses


def _public_image_url(url: str) -> bool:
    """Require HTTPS and reject literal private targets (which bypass DNS)."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or any(ord(char) <= 32 for char in url)
        ):
            return False
        try:
            return ip_address(parsed.hostname).is_global
        except ValueError:
            return True
    except ValueError:
        return False


@callback
def async_generate_image_proxy_url(
    config_entry_id: str, serial: str, reference: str
) -> str:
    """Generate a URL for an opaque server-owned image reference, never a URL."""
    return ImageProxyView.url.format(
        config_entry_id=config_entry_id,
        serial=serial,
        url=reference,
    )


@callback
def _encryption_key(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> str | None:
    """Prefer a usable runtime key, otherwise the private camera options."""
    return (
        (entry.options.get(OPTIONS_KEY_CAMERAS, {}) or {})
        .get(serial, {})
        .get(CONF_ENC_KEY)
    )


class ImageProxyView(HomeAssistantView):
    """View to proxy and decrypt Ezviz alarm images."""

    requires_auth = True
    url = "/api/ezviz_cloud/image/{config_entry_id}/{serial}/{url}"
    name = "api:ezviz_cloud_image"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the proxy view."""
        self.hass = hass
        # A dedicated connector enforces public-only DNS at connection time;
        # HA's pooled connector cannot accept a custom resolver.
        self.session = ClientSession(
            connector=TCPConnector(resolver=PublicImageResolver())
        )

        async def close_session(_event: Event) -> None:
            await self.session.close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, close_session)

    async def get(
        self, request: web.Request, config_entry_id: str, serial: str, url: str
    ) -> web.StreamResponse:
        """Return decrypted image bytes for an alarm picture."""
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
                text="EZVIZ integration is unavailable",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        record = coordinator.image_records.get(url)
        if (
            serial not in (coordinator.data or {})
            or record is None
            or record[0] != serial
        ):
            return web.Response(text="Unknown image", status=HTTPStatus.NOT_FOUND)
        raw_url = record[1]
        if not _public_image_url(raw_url):
            return web.Response(
                text="Invalid image target", status=HTTPStatus.BAD_REQUEST
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
                allow_redirects=False,
                timeout=ClientTimeout(
                    total=30, connect=10, sock_connect=10, sock_read=20
                ),
            )
            if resp.status != HTTPStatus.OK:
                return web.Response(
                    text="Unable to fetch image", status=HTTPStatus.BAD_GATEWAY
                )
            body = await resp.read()
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Error fetching EZVIZ image (%s)", type(err).__name__)
            return web.Response(
                text="Unable to fetch image", status=HTTPStatus.BAD_REQUEST
            )
        finally:
            if resp is not None:
                resp.release()

        try:
            # Media browser defaults to the first SDK lens, with or without a key.
            body = await coordinator.async_execute(
                process_image_bytes, body, enc_key, 1, 1
            )
        except HomeAssistantError:
            return web.Response(
                text="EZVIZ integration is unavailable",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except PyEzvizError:
            return web.Response(
                text="Unable to decode image", status=HTTPStatus.BAD_REQUEST
            )
        content_type = (
            "image/jpeg" if body.startswith(b"\xff\xd8\xff") else infer_image_type(body)
        )
        if _has_residual_encryption(body) or content_type is None:
            return web.Response(
                text="Unable to decode image", status=HTTPStatus.BAD_REQUEST
            )
        return web.Response(body=body, content_type=content_type)
