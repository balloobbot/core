"""Milesight devices on a LoRaWAN provider."""

from homeassistant.components.lorawan import DeviceManager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from ._vendor.milesight_lorawan import MilesightDevice, MilesightDevices
from .const import DOMAIN
from .coordinator import MilesightCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type MilesightConfigEntry = ConfigEntry[
    DeviceManager[MilesightDevice, MilesightCoordinator]
]
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: MilesightConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    manager = entry.runtime_data = DeviceManager(
        hass,
        entry,
        create_collection=MilesightDevices,
        create_coordinator=MilesightCoordinator,
    )
    entry.async_on_unload(manager.close)
    await manager.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MilesightConfigEntry) -> bool:
    """Unload entities; the manager releases the collection and coordinators."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
