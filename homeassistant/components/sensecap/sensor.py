"""Sensor mapping from library device state."""

from typing import override

from homeassistant.components.lorawan import LoRaWANEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, SenseCapConfigEntry
from ._vendor.sensecap_lorawan import S2101
from .coordinator import SenseCapCoordinator

DESCRIPTIONS = (
    SensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SenseCapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Listen for models, including those created during initial inventory."""

    @callback
    def added(device: S2101) -> None:
        async_add_entities(
            SenseCapSensor(
                entry.runtime_data.coordinators[device.descriptor.dev_eui], description
            )
            for description in DESCRIPTIONS
        )

    entry.async_on_unload(entry.runtime_data.collection.subscribe_device_added(added))


class SenseCapSensor(LoRaWANEntity[S2101], SensorEntity):
    """Read typed state; decoding and event interpretation belong to the library."""

    def __init__(
        self, coordinator: SenseCapCoordinator, description: SensorEntityDescription
    ) -> None:
        """Bind one measurement to its model."""
        super().__init__(coordinator)
        self.entity_description = description
        descriptor = self.device.descriptor
        identity = f"{descriptor.network_id}:{descriptor.dev_eui}"
        self._attr_unique_id = f"{identity}:channel_1:{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=descriptor.name,
            manufacturer="Seeed Studio",
            model="SenseCAP S2101",
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the last observed measurement."""
        return getattr(self.device, self.entity_description.key)
