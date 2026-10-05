"""Shared LoRaWAN connections, discovery and device lifecycle."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_lorawan

from .connection import (
    DATA_REGISTRY,
    ConnectionRegistry,
    async_get_connections as async_get_connections,
    async_register_connection as async_register_connection,
)
from .const import DOMAIN
from .device_manager import (
    DeviceManager as DeviceManager,
    device_identifier as device_identifier,
)
from .entity import LoRaWANEntity as LoRaWANEntity
from .websocket_api import async_register

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize the shared registry without configuring a network server."""
    hass.data[DATA_REGISTRY] = ConnectionRegistry(await async_get_lorawan(hass))
    async_register(hass)
    return True
