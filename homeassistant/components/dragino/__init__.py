"""Dragino devices on a LoRaWAN provider."""

from homeassistant.components.lorawan import DeviceManager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from ._vendor.dragino_lorawan import LT22222, DraginoDevices
from .const import DOMAIN
from .coordinator import DraginoCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type DraginoConfigEntry = ConfigEntry[DeviceManager[LT22222, DraginoCoordinator]]
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    manager = entry.runtime_data = DeviceManager(
        hass,
        entry,
        create_collection=DraginoDevices,
        create_coordinator=DraginoCoordinator,
    )
    entry.async_on_unload(manager.close)
    await manager.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Unload entities; the manager releases the collection and coordinators."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
