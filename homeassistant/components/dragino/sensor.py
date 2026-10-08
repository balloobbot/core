"""Measurements and counters exposed by Dragino library models."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.lorawan import LoRaWANEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DraginoConfigEntry
from ._vendor.dragino_lorawan import LHT65, LT22222, DraginoDevice
from .coordinator import DraginoCoordinator
from .entity import DraginoEntity, async_setup_entities

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DraginoSensorDescription(SensorEntityDescription):
    """Describe a measurement exposed by a model."""

    channels: tuple[int, ...]
    value_fn: Callable[[LT22222, int], float | int | None]


SENSORS = (
    DraginoSensorDescription(
        key="voltage",
        channels=(1, 2),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device, channel: device.voltages[channel],
    ),
    DraginoSensorDescription(
        key="current",
        channels=(1, 2),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device, channel: device.currents[channel],
    ),
    DraginoSensorDescription(
        key="digital_count",
        channels=(1, 2),
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda device, channel: device.digital_counts[channel],
    ),
    DraginoSensorDescription(
        key="voltage_count",
        channels=(1,),
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda device, channel: device.voltage_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create measurement entities for the collection."""
    async_setup_entities(
        hass,
        entry,
        async_add_entities,
        lambda coordinator: [
            DraginoSensor(coordinator, description, channel)
            for description in SENSORS
            for channel in description.channels
        ],
    )

    @callback
    def added(coordinator: DraginoCoordinator) -> None:
        if isinstance(coordinator.data, LHT65):
            async_add_entities(
                LHT65Sensor(coordinator, description) for description in LHT65_SENSORS
            )

    entry.async_on_unload(entry.runtime_data.subscribe_coordinator_added(added))


class DraginoSensor(DraginoEntity, SensorEntity):
    """Read a measurement without decoding device messages."""

    entity_description: DraginoSensorDescription

    def __init__(
        self,
        coordinator: DraginoCoordinator,
        description: DraginoSensorDescription,
        channel: int,
    ) -> None:
        """Bind a measurement description and channel."""
        super().__init__(coordinator, description.key, channel)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self.device, self.channel)


@dataclass(frozen=True, kw_only=True)
class LHT65SensorDescription(SensorEntityDescription):
    """Describe a value supplied by the LHT65 model."""

    value_fn: Callable[[LHT65], float | int | None]


LHT65_SENSORS = (
    LHT65SensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.temperature,
    ),
    LHT65SensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.humidity,
    ),
    LHT65SensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.battery_voltage,
    ),
    LHT65SensorDescription(
        key="external_temperature",
        translation_key="external_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.external_temperature,
    ),
    LHT65SensorDescription(
        key="illuminance",
        translation_key="illuminance",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=LIGHT_LUX,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.illuminance,
    ),
    LHT65SensorDescription(
        key="external_voltage",
        translation_key="external_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.external_voltage,
    ),
    LHT65SensorDescription(
        key="external_count",
        translation_key="external_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda device: device.external_count,
    ),
)


class LHT65Sensor(LoRaWANEntity[DraginoDevice], SensorEntity):
    """Read LHT65 measurements from a shared model."""

    coordinator: DraginoCoordinator
    entity_description: LHT65SensorDescription

    def __init__(
        self, coordinator: DraginoCoordinator, description: LHT65SensorDescription
    ) -> None:
        """Bind a measurement to its device model."""
        super().__init__(coordinator)
        self.entity_description = description
        descriptor = self.device.descriptor
        self._attr_unique_id = (
            f"{descriptor.network_id}:{descriptor.dev_eui}:{description.key}"
        )
        self._attr_device_info = coordinator.device_info

    @property
    @override
    def native_value(self) -> float | int | None:
        device = self.device
        assert isinstance(device, LHT65)
        return self.entity_description.value_fn(device)
