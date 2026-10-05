"""Share device model updates across all platforms."""

import logging
from typing import override

from homeassistant.components.lorawan import device_identifiers
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ._vendor.sensecap_lorawan import S2101
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SenseCapCoordinator(DataUpdateCoordinator[S2101]):
    """Distribute push updates from one physical device."""

    def __init__(self, hass: HomeAssistant, device: S2101) -> None:
        """Subscribe once to the library model."""
        # The device manager shuts down coordinators when their models retire.
        super().__init__(hass, _LOGGER, config_entry=None, name=device.descriptor.name)
        self.async_set_updated_data(device)
        self._unsubscribe = device.add_update_listener(self._async_device_updated)

    @property
    def device_info(self) -> DeviceInfo:
        """Describe this device for its entities."""
        return DeviceInfo(
            identifiers=device_identifiers(DOMAIN, self.data),
            name=self.data.descriptor.name,
            manufacturer="Seeed Studio",
            model="SenseCAP S2101",
        )

    @callback
    def _async_device_updated(self) -> None:
        """Forward the model's complete update to every entity."""
        self.async_set_updated_data(self.data)

    @override
    async def async_shutdown(self) -> None:
        """Release the model subscription when its collection retires it."""
        self._unsubscribe()
        await super().async_shutdown()
