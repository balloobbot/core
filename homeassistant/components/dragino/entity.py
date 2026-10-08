"""Shared Dragino entity identity and collection subscriptions."""

from collections.abc import Callable
from typing import override

from homeassistant.components.lorawan import LoRaWANEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222, DraginoDevice
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
    def added(coordinator: DraginoCoordinator) -> None:
        if isinstance(coordinator.data, LT22222):
            async_add_entities(factory(coordinator))

    entry.async_on_unload(entry.runtime_data.subscribe_coordinator_added(added))


class DraginoEntity(LoRaWANEntity[DraginoDevice]):
    """Observe a library model without interpreting LoRaWAN messages."""

    coordinator: DraginoCoordinator

    def __init__(self, coordinator: DraginoCoordinator, key: str, channel: int) -> None:
        """Bind a channel to a model and its registry identity."""
        super().__init__(coordinator)
        self.channel = channel
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"channel": str(channel)}
        descriptor = self.device.descriptor
        identity = f"{descriptor.network_id}:{descriptor.dev_eui}"
        self._attr_unique_id = f"{identity}:{key}_{channel}"

    @property
    @override
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    @override
    def device(self) -> LT22222:
        """This entity factory only creates LT-22222-L entities."""
        device = self.coordinator.data
        assert isinstance(device, LT22222)
        return device
