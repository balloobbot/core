"""Relay entities backed by Dragino library models."""

from asyncio import timeout
from typing import Any, override

from lorawan_connection import DownlinkError

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, DraginoConfigEntry
from ._vendor.dragino_lorawan import LT22222


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create relay entities when models appear, including initial inventory."""
    entities: dict[str, list[DraginoRelay]] = {}

    @callback
    def added(device: LT22222) -> None:
        relays = entities[device.descriptor.dev_eui] = [
            DraginoRelay(device, channel) for channel in device.relays
        ]
        async_add_entities(relays)

    @callback
    def removed(device: LT22222) -> None:
        for entity in entities.pop(device.descriptor.dev_eui, []):
            entity.retired = True
            if entity.hass is not None:
                entity.async_write_ha_state()
                entry.async_create_task(
                    hass, entity.async_remove(force_remove=True), "Remove Dragino relay"
                )

    entry.async_on_unload(entry.runtime_data.subscribe_device_added(added))
    entry.async_on_unload(entry.runtime_data.subscribe_device_removed(removed))


class DraginoRelay(SwitchEntity):
    """Observe one relay; the model encodes commands and decodes reported state."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "relay"

    def __init__(self, device: LT22222, channel: int) -> None:
        """Bind one relay channel to its device model."""
        self.device = device
        self.channel = channel
        self.retired = False
        self._attr_translation_placeholders = {"channel": str(channel)}
        descriptor = device.descriptor
        identity = f"{descriptor.network_id}:{descriptor.dev_eui}"
        self._attr_unique_id = f"{identity}:relay_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=descriptor.name,
            manufacturer="Dragino",
            model="LT-22222-L",
        )

    @property
    @override
    def is_on(self) -> bool | None:
        return self.device.relays[self.channel]

    @property
    @override
    def available(self) -> bool:
        return not self.retired and not self.device.closed

    @override
    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.device.add_update_listener(self.async_write_ha_state))

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_relay(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_relay(False)

    async def _async_set_relay(self, on: bool) -> None:
        try:
            async with timeout(30):
                await self.device.async_set_relay(self.channel, on)
        except TimeoutError as error:
            raise HomeAssistantError(
                "Timed out waiting for device acknowledgement"
            ) from error
        except DownlinkError as error:
            raise HomeAssistantError(str(error)) from error
