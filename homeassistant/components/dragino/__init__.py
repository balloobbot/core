"""Dragino devices on a LoRaWAN provider."""

from dataclasses import dataclass, field

from homeassistant.components.lorawan import ConnectionUnavailable, get_connection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._vendor.dragino_lorawan import LT22222, DraginoDevices
from .coordinator import DraginoCoordinator

DOMAIN = "dragino"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type DraginoConfigEntry = ConfigEntry[DraginoData]
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


@dataclass
class DraginoData:
    """Keep the collection and its shared device coordinators."""

    collection: DraginoDevices
    coordinators: dict[str, DraginoCoordinator] = field(default_factory=dict)


async def async_setup_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    try:
        connection = get_connection(hass, entry.data["provider_entry_id"])
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
    devices = DraginoDevices(connection)
    entry.runtime_data = DraginoData(devices)

    @callback
    def added(device: LT22222) -> None:
        entry.runtime_data.coordinators[device.descriptor.dev_eui] = DraginoCoordinator(
            hass, device
        )

    @callback
    def removed(device: LT22222) -> None:
        coordinator = entry.runtime_data.coordinators.pop(device.descriptor.dev_eui)
        entry.async_create_task(
            hass, coordinator.async_shutdown(), "Stop device coordinator"
        )

    entry.async_on_unload(devices.subscribe_device_added(added))
    entry.async_on_unload(devices.subscribe_device_removed(removed))
    entry.async_on_unload(devices.close)
    try:
        await devices.async_setup()
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Unload entities; entry callbacks release collection and subscription."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
