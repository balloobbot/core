"""Sensor mapping from library device state."""

from typing import override

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
    entities: dict[str, list[SenseCapSensor]] = {}

    @callback
    def added(device: S2101) -> None:
        sensors = entities[device.descriptor.dev_eui] = [
            SenseCapSensor(device, description) for description in DESCRIPTIONS
        ]
        async_add_entities(sensors)

    @callback
    def removed(device: S2101) -> None:
        for entity in entities.pop(device.descriptor.dev_eui, []):
            entity.retired = True
            if entity.hass is not None:
                entity.async_write_ha_state()
                entry.async_create_task(
                    hass,
                    entity.async_remove(force_remove=True),
                    "Remove SenseCAP entity",
                )

    entry.async_on_unload(entry.runtime_data.subscribe_device_added(added))
    entry.async_on_unload(entry.runtime_data.subscribe_device_removed(removed))


class SenseCapSensor(SensorEntity):
    """Read typed state; decoding and event interpretation belong to the library."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: S2101, description: SensorEntityDescription) -> None:
        """Bind one measurement to its model."""
        self.device = device
        self.entity_description = description
        self.retired = False
        descriptor = device.descriptor
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
        return getattr(self.device.state, self.entity_description.key)

    @property
    @override
    def available(self) -> bool:
        """Normal device sleep does not mean unavailable."""
        return not self.retired and not self.device.closed

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe only while this entity is loaded."""

        self.async_on_remove(self.device.add_update_listener(self.async_write_ha_state))
