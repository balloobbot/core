"""SenseCAP devices on a LoRaWAN provider."""

from dataclasses import dataclass, field

from homeassistant.components.lorawan import (
    ConnectionUnavailable,
    ProviderNotFound,
    get_connection,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr

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
    devices = SenseCapDeviceCollection(connection)
    entry.runtime_data = SenseCapData(devices)

    device_registry = dr.async_get(hass)

    @callback
    def added(device: S2101) -> None:
        entry.runtime_data.coordinators[device.descriptor.dev_eui] = (
            SenseCapCoordinator(hass, device)
        )

        descriptor = device.descriptor
        registered = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{descriptor.network_id}:{descriptor.dev_eui}")},
            name=descriptor.name,
            manufacturer="Seeed Studio",
            model="SenseCAP S2101",
        )

        @callback
        def update_name() -> None:
            device_registry.async_update_device(
                registered.id, name=device.descriptor.name
            )

        @callback
        def remove_device() -> None:
            device_registry.async_remove_device(registered.id)

        device.add_update_listener(update_name)
        device.add_remove_listener(remove_device)

    @callback
    def removed(device: S2101) -> None:
        coordinator = entry.runtime_data.coordinators.pop(device.descriptor.dev_eui)
        entry.async_create_task(
            hass, coordinator.async_shutdown(), "Stop device coordinator"
        )

    entry.async_on_unload(devices.subscribe_device_added(added))
    entry.async_on_unload(devices.subscribe_device_removed(removed))
    # Unload runs in reverse order: retire models before removing collection listeners.
    entry.async_on_unload(devices.close)
    try:
        await devices.async_setup()
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    current = {
        (DOMAIN, f"{device.descriptor.network_id}:{device.descriptor.dev_eui}")
        for device in devices.devices.values()
    }
    for registered in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        if not registered.identifiers.intersection(current):
            device_registry.async_remove_device(registered.id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Unload entities; entry callbacks release collection and subscription."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
