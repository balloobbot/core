"""Measurements and counters exposed by Dragino library models."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222
from .coordinator import DraginoCoordinator
from .entity import DraginoEntity, async_setup_entities


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
