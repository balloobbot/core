"""SenseCAP devices on a LoRaWAN provider."""

from homeassistant.components.lorawan import (
    ConnectionNotFound,
    ConnectionUnavailable,
    DeviceManager,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._vendor.sensecap_lorawan import S2101, SenseCapDeviceCollection
from .const import DOMAIN
from .coordinator import SenseCapCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type SenseCapConfigEntry = ConfigEntry[DeviceManager[S2101, SenseCapCoordinator]]
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    manager = entry.runtime_data = DeviceManager(
        hass,
        entry,
        create_collection=SenseCapDeviceCollection,
        create_coordinator=SenseCapCoordinator,
    )
    entry.async_on_unload(manager.close)
    try:
        await manager.async_setup(connection_entry_id=entry.data["connection_entry_id"])
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN connection is not available") from error
    except ConnectionNotFound as error:
        raise ConfigEntryError(
            "The selected LoRaWAN connection entry was removed"
        ) from error
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Unload entities; the manager releases the collection and coordinators."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Rename the reference to the LoRaWAN connection entry."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        data = dict(entry.data)
        if "provider_entry_id" in data:
            data["connection_entry_id"] = data.pop("provider_entry_id")
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True
