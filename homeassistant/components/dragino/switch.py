"""Output entities backed by Dragino library models."""

from asyncio import timeout
from typing import Any, Literal, override

from lorawan_connection import DownlinkError

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, DraginoConfigEntry
from .coordinator import DraginoCoordinator
from .entity import DraginoEntity, async_setup_entities

# ChirpStack queues commands; waiting for one ACK must not block other devices.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DraginoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create output entities when models appear, including initial inventory."""
    async_setup_entities(
        hass,
        entry,
        async_add_entities,
        lambda coordinator: [
            DraginoOutput(coordinator, kind, channel)
            for kind in ("relay", "digital_output")
            for channel in (1, 2)
        ],
    )


class DraginoOutput(DraginoEntity, SwitchEntity):
    """Read and control one output through the device model."""

    def __init__(
        self,
        coordinator: DraginoCoordinator,
        kind: Literal["relay", "digital_output"],
        channel: int,
    ) -> None:
        """Bind an output channel and its command method."""
        super().__init__(coordinator, kind, channel)
        self.kind = kind

    @property
    @override
    def is_on(self) -> bool | None:
        if self.kind == "relay":
            return self.device.relays[self.channel]
        return self.device.digital_outputs[self.channel]

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_output(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_output(False)

    async def _async_set_output(self, on: bool) -> None:
        command = (
            self.device.async_set_relay
            if self.kind == "relay"
            else self.device.async_set_digital_output
        )
        try:
            async with timeout(30):
                await command(self.channel, on)
        except TimeoutError as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
            ) from error
        except DownlinkError as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from error
