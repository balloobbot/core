"""Dragino devices on a LoRaWAN provider."""

from homeassistant.components.lorawan import (
    ConnectionUnavailable,
    DeviceManager,
    ProviderNotFound,
    async_get_connection,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._vendor.dragino_lorawan import LT22222, DraginoDevices
from .const import DOMAIN
from .coordinator import DraginoCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type DraginoConfigEntry = ConfigEntry[DeviceManager[LT22222, DraginoCoordinator]]
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    try:
        connection = async_get_connection(hass, entry.data["connection_entry_id"])
    except ProviderNotFound as error:
        raise ConfigEntryError("The selected LoRaWAN provider was removed") from error
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    entry.async_on_unload(
        connection.on_disconnect(
            lambda: (
                None
                if hass.is_stopping
                else hass.config_entries.async_schedule_reload(entry.entry_id)
            )
        )
    )
    manager = entry.runtime_data = DeviceManager(
        hass,
        entry,
        collection=DraginoDevices(connection),
        create_coordinator=DraginoCoordinator,
    )
    entry.async_on_unload(manager.close)
    try:
        await manager.async_setup()
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Unload entities; the manager releases the collection and coordinators."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Rename the reference to the LoRaWAN connection entry."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        data = dict(entry.data)
        if "provider_entry_id" in data:
            data["connection_entry_id"] = data.pop("provider_entry_id")
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True
