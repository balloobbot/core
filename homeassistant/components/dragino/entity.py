"""Shared Dragino entity identity and collection subscriptions."""

from collections.abc import Callable
from typing import override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222


@callback
def async_setup_entities(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[LT22222], list[DraginoEntity]],
) -> None:
    """Add existing and future models and retire entities with their model."""
    entities: dict[str, list[DraginoEntity]] = {}

    @callback
    def added(device: LT22222) -> None:
        entities[device.descriptor.dev_eui] = new_entities = factory(device)
        async_add_entities(new_entities)

    @callback
    def removed(device: LT22222) -> None:
        for entity in entities.pop(device.descriptor.dev_eui, []):
            entity.retired = True
            if entity.hass is not None:
                entity.async_write_ha_state()
                entry.async_create_task(
                    hass,
                    entity.async_remove(force_remove=True),
                    "Remove Dragino entity",
                )

    entry.async_on_unload(entry.runtime_data.subscribe_device_added(added))
    entry.async_on_unload(entry.runtime_data.subscribe_device_removed(removed))


class DraginoEntity(Entity):
    """Observe a library model without interpreting LoRaWAN messages."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: LT22222, key: str, channel: int) -> None:
        """Bind a channel to a model and its registry identity."""
        self.device = device
        self.channel = channel
        self.retired = False
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"channel": str(channel)}
        descriptor = device.descriptor
        identity = f"{descriptor.network_id}:{descriptor.dev_eui}"
        self._attr_unique_id = f"{identity}:{key}_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=descriptor.name,
            manufacturer="Dragino",
            model="LT-22222-L",
        )

    @property
    @override
    def available(self) -> bool:
        return not self.retired and not self.device.closed

    @override
    async def async_added_to_hass(self) -> None:
        if self.retired or self.device.closed:
            self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        self.async_on_remove(self.device.add_update_listener(self.async_write_ha_state))
