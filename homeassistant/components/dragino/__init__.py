"""Dragino devices on a LoRaWAN provider."""

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
        connection = get_connection(hass, entry.data["connection_entry_id"])
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
    devices = DraginoDevices(connection)
    entry.runtime_data = DraginoData(devices)

    device_registry = dr.async_get(hass)

    @callback
    def added(device: LT22222) -> None:
        entry.runtime_data.coordinators[device.descriptor.dev_eui] = DraginoCoordinator(
            hass, device
        )

        descriptor = device.descriptor
        registered = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{descriptor.network_id}:{descriptor.dev_eui}")},
            name=descriptor.name,
            manufacturer="Dragino",
            model="LT-22222-L",
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
    def removed(device: LT22222) -> None:
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


async def async_unload_entry(hass: HomeAssistant, entry: DraginoConfigEntry) -> bool:
    """Unload entities; entry callbacks release collection and subscription."""
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
