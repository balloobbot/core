"""Measurements and reported valve states from Milesight models."""

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
    PERCENTAGE,
    EntityCategory,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MilesightConfigEntry
from ._vendor.milesight_lorawan import TS201, MilesightDevice, UC51x
from .coordinator import MilesightCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MilesightSensorDescription(SensorEntityDescription):
    """Bind a supported model to one reported value."""

    model_type: type[MilesightDevice]
    value_fn: Callable[[MilesightDevice], str | float | int | None]


def valve_state(device: MilesightDevice, channel: int) -> str | None:
    """Expose reported state without sending valve commands."""
    assert isinstance(device, UC51x)
    value = device.valve_1_open if channel == 1 else device.valve_2_open
    return None if value is None else "open" if value else "closed"


DESCRIPTIONS = (
    MilesightSensorDescription(
        key="temperature",
        translation_key="temperature",
        model_type=TS201,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: (
            device.temperature if isinstance(device, TS201) else None
        ),
    ),
    MilesightSensorDescription(
        key="battery",
        translation_key="battery",
        model_type=MilesightDevice,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.reported_battery_percent,
    ),
    MilesightSensorDescription(
        key="valve_1",
        translation_key="valve_1",
        model_type=UC51x,
        device_class=SensorDeviceClass.ENUM,
        options=["open", "closed"],
        value_fn=lambda device: valve_state(device, 1),
    ),
    MilesightSensorDescription(
        key="valve_2",
        translation_key="valve_2",
        model_type=UC51x,
        device_class=SensorDeviceClass.ENUM,
        options=["open", "closed"],
        value_fn=lambda device: valve_state(device, 2),
    ),
    MilesightSensorDescription(
        key="pulse_count_1",
        translation_key="pulse_count_1",
        model_type=UC51x,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda device: (
            device.valve_1_pulse_count if isinstance(device, UC51x) else None
        ),
    ),
    MilesightSensorDescription(
        key="pulse_count_2",
        translation_key="pulse_count_2",
        model_type=UC51x,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda device: (
            device.valve_2_pulse_count if isinstance(device, UC51x) else None
        ),
    ),
    MilesightSensorDescription(
        key="pressure",
        translation_key="pressure",
        model_type=UC51x,
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.KPA,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda device: (
            device.pressure_kpa if isinstance(device, UC51x) else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MilesightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Subscribe to existing models and later inventory additions."""

    @callback
    def added(coordinator: MilesightCoordinator) -> None:
        async_add_entities(
            MilesightSensor(coordinator, description)
            for description in DESCRIPTIONS
            if isinstance(coordinator.data, description.model_type)
        )

    entry.async_on_unload(entry.runtime_data.subscribe_coordinator_added(added))


class MilesightSensor(LoRaWANEntity[MilesightDevice], SensorEntity):
    """Read one value from the shared model."""

    coordinator: MilesightCoordinator
    entity_description: MilesightSensorDescription

    def __init__(
        self, coordinator: MilesightCoordinator, description: MilesightSensorDescription
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
    def native_value(self) -> str | float | int | None:
        return self.entity_description.value_fn(self.device)
