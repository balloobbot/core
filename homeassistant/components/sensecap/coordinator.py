"""Share device model updates across all platforms."""

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ._vendor.sensecap_lorawan import S2101

if TYPE_CHECKING:
    from . import SenseCapConfigEntry

_LOGGER = logging.getLogger(__name__)


class SenseCapCoordinator(DataUpdateCoordinator[S2101]):
    """Distribute push updates from one physical device."""

    def __init__(
        self, hass: HomeAssistant, entry: SenseCapConfigEntry, device: S2101
    ) -> None:
        """Subscribe once to the library model."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=device.descriptor.name)
        self.async_set_updated_data(device)
        entry.async_on_unload(device.add_update_listener(self._async_device_updated))

    @callback
    def _async_device_updated(self) -> None:
        """Forward the model's complete update to every entity."""
        self.async_set_updated_data(self.data)
