"""Shared lifecycle for entities backed by LoRaWAN device models."""

from typing import override

from lorawan_connection import Device

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)


class LoRaWANEntity[DeviceT: Device](CoordinatorEntity[DataUpdateCoordinator[DeviceT]]):
    """Share model updates and remove entities when their device is removed."""

    _attr_has_entity_name = True

    @property
    def device(self) -> DeviceT:
        """Return the model shared by this device's entities."""
        return self.coordinator.data

    @property
    @override
    def available(self) -> bool:
        return super().available and not self.device.closed

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.device.add_remove_listener(self._async_device_removed)
        )

    @callback
    def _async_device_removed(self) -> None:
        """Retire the active entity while retaining its registry record."""
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_remove(force_remove=True))

    @override
    async def async_update(self) -> None:
        """Values arrive through the library's subscription."""
