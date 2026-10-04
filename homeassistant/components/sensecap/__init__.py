"""SenseCAP devices on a LoRaWAN provider."""

from dataclasses import dataclass, field

from homeassistant.components.lorawan import ConnectionUnavailable, get_connection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._vendor.sensecap_lorawan import S2101, SenseCapDeviceCollection
from .coordinator import SenseCapCoordinator

DOMAIN = "sensecap"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type SenseCapConfigEntry = ConfigEntry[SenseCapData]
PLATFORMS = [Platform.SENSOR]


@dataclass
class SenseCapData:
    """Keep the collection and its shared device coordinators."""

    collection: SenseCapDeviceCollection
    coordinators: dict[str, SenseCapCoordinator] = field(default_factory=dict)


async def async_setup_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
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
    devices = SenseCapDeviceCollection(connection)
    entry.runtime_data = SenseCapData(devices)

    @callback
    def added(device: S2101) -> None:
        entry.runtime_data.coordinators[device.descriptor.dev_eui] = (
            SenseCapCoordinator(hass, device)
        )

    @callback
    def removed(device: S2101) -> None:
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


async def async_unload_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Unload entities; entry callbacks release collection and subscription."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
