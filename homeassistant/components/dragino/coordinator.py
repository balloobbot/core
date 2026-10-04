"""Share device model updates across all platforms."""

import logging
from typing import override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ._vendor.dragino_lorawan import LT22222

_LOGGER = logging.getLogger(__name__)


class DraginoCoordinator(DataUpdateCoordinator[LT22222]):
    """Distribute push updates from one physical device."""

    def __init__(self, hass: HomeAssistant, device: LT22222) -> None:
        """Subscribe once to the library model."""
        super().__init__(hass, _LOGGER, config_entry=None, name=device.descriptor.name)
        self.async_set_updated_data(device)
        self._unsubscribe = device.add_update_listener(self._async_device_updated)

    @callback
    def _async_device_updated(self) -> None:
        """Forward the model's complete update to every entity."""
        self.async_set_updated_data(self.data)

    @override
    async def async_shutdown(self) -> None:
        """Release the model subscription when its collection retires it."""
        self._unsubscribe()
        await super().async_shutdown()
