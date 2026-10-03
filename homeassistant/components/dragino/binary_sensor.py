"""Digital inputs observed through Dragino library models."""

from typing import override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222
from .entity import DraginoEntity, async_setup_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create digital input entities for the collection."""
    async_setup_entities(
        hass,
        entry,
        async_add_entities,
        lambda device: [DraginoDigitalInput(device, channel) for channel in (1, 2)],
    )


class DraginoDigitalInput(DraginoEntity, BinarySensorEntity):
    """Read a digital input level; counting modes do not report levels."""

    def __init__(self, device: LT22222, channel: int) -> None:
        """Bind a digital input channel."""
        super().__init__(device, "digital_input", channel)

    @property
    @override
    def is_on(self) -> bool | None:
        return self.device.digital_inputs[self.channel]
