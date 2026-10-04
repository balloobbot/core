"""Shared Dragino entity identity and collection subscriptions."""

from collections.abc import Callable

from homeassistant.components.lorawan import LoRaWANEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222
from .coordinator import DraginoCoordinator


@callback
def async_setup_entities(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[DraginoCoordinator], list[DraginoEntity]],
) -> None:
    """Add entities for existing and future models."""

    @callback
    def added(device: LT22222) -> None:
        async_add_entities(
            factory(entry.runtime_data.coordinators[device.descriptor.dev_eui])
        )

    entry.async_on_unload(entry.runtime_data.collection.subscribe_device_added(added))


class DraginoEntity(LoRaWANEntity[LT22222]):
    """Observe a library model without interpreting LoRaWAN messages."""

    def __init__(self, coordinator: DraginoCoordinator, key: str, channel: int) -> None:
        """Bind a channel to a model and its registry identity."""
        super().__init__(coordinator)
        self.channel = channel
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"channel": str(channel)}
        descriptor = self.device.descriptor
        identity = f"{descriptor.network_id}:{descriptor.dev_eui}"
        self._attr_unique_id = f"{identity}:{key}_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=descriptor.name,
            manufacturer="Dragino",
            model="LT-22222-L",
        )
