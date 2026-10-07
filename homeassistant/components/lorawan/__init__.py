"""Shared LoRaWAN connections, discovery and device lifecycle."""

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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
    registry = hass.data[DATA_REGISTRY] = ConnectionRegistry(
        await async_get_lorawan(hass)
    )

    @callback
    def entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if change is ConfigEntryChange.REMOVED:
            if collection := registry.inventories.pop(entry.entry_id, None):
                collection.close()

    unsubscribe = async_dispatcher_connect(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, entry_changed
    )

    @callback
    def shutdown(event: Event) -> None:
        unsubscribe()
        for collection in registry.inventories.values():
            collection.close()
        registry.inventories.clear()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    async_register(hass)
    return True
