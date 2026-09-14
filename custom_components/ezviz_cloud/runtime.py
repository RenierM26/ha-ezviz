"""Typed resources owned by a loaded EZVIZ entry."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from pyezvizapi.client import EzvizClient

    from .coordinator import EzvizDataUpdateCoordinator
    from .mqtt import EzvizMqttHandler
    from .token_store import EzvizTokenStore


@dataclass
class EzvizRuntimeData:
    """One owner for entry resources; credential locks remain domain-scoped."""

    client: "EzvizClient"
    coordinator: "EzvizDataUpdateCoordinator"
    push: "EzvizMqttHandler"
    token_store: "EzvizTokenStore"


type EzvizConfigEntry = ConfigEntry[EzvizRuntimeData]
