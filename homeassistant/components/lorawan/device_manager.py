"""Manage vendor device collections and their Home Assistant coordinators."""

from collections.abc import Callable
import logging
from typing import Any

from lorawan_connection import Device, DeviceCollection, Unsubscribe

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def device_identifiers(domain: str, device: Device) -> set[tuple[str, str]]:
    """Identify a physical device within its vendor integration and network."""
    descriptor = device.descriptor
    return {(domain, f"{descriptor.network_id}:{descriptor.dev_eui}")}


class DeviceManager[DeviceT: Device, CoordinatorT: DataUpdateCoordinator[Any]]:
    """Own collection subscriptions, coordinators, and registry cleanup."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        collection: DeviceCollection[DeviceT],
        create_coordinator: Callable[[HomeAssistant, DeviceT], CoordinatorT],
    ) -> None:
        """Register collection callbacks before models are created."""
        self.collection = collection
        self.coordinators: dict[str, CoordinatorT] = {}
        self._hass = hass
        self._entry = entry
        self._registry = dr.async_get(hass)
        self._create_coordinator = create_coordinator
        self._listeners: list[Callable[[CoordinatorT], None]] = []
        self._closed = False
        self._unsubscribe_removed = collection.subscribe_device_removed(
            self._device_removed
        )
        self._unsubscribe_added = collection.subscribe_device_added(self._device_added)

    async def async_setup(self) -> None:
        """Subscribe to devices, then remove registry records absent from the snapshot."""
        try:
            await self.collection.async_setup()
        except BaseException:
            self.close()
            raise
        # Collection callbacks isolate failures; do not reconcile a partial setup.
        if self.coordinators.keys() != self.collection.devices.keys():
            self.close()
            raise HomeAssistantError("Failed to create a LoRaWAN device coordinator")
        current = {
            identifier
            for device in self.collection.devices.values()
            for identifier in device_identifiers(self._entry.domain, device)
        }
        for registered in dr.async_entries_for_config_entry(
            self._registry, self._entry.entry_id
        ):
            if not registered.identifiers.intersection(current):
                self._registry.async_remove_device(registered.id)

    @callback
    def subscribe_coordinator_added(
        self, listener: Callable[[CoordinatorT], None]
    ) -> Unsubscribe:
        """Deliver existing coordinators immediately, followed by future additions."""
        if self._closed:
            raise RuntimeError("Device manager is closed")

        def forward(coordinator: CoordinatorT) -> None:
            listener(coordinator)

        self._listeners.append(forward)

        def unsubscribe() -> None:
            if forward in self._listeners:
                self._listeners.remove(forward)

        try:
            for dev_eui, coordinator in tuple(self.coordinators.items()):
                if self.coordinators.get(dev_eui) is coordinator:
                    forward(coordinator)
        except BaseException:
            unsubscribe()
            raise
        return unsubscribe

    @callback
    def _device_added(self, device: DeviceT) -> None:
        coordinator = self._create_coordinator(self._hass, device)
        self.coordinators[device.descriptor.dev_eui] = coordinator
        device.add_update_listener(lambda: self._update_name(device))
        device.add_remove_listener(lambda: self._remove_registry_device(device))
        for listener in tuple(self._listeners):
            if listener in self._listeners:
                try:
                    listener(coordinator)
                except Exception:
                    _LOGGER.exception(
                        "Failed to add entities for a LoRaWAN coordinator"
                    )

    def _registry_device(self, device: DeviceT) -> dr.DeviceEntry | None:
        identifier = next(iter(device_identifiers(self._entry.domain, device)))
        return self._registry.async_get_device_by_identifier(
            identifier, self._entry.entry_id
        )

    @callback
    def _update_name(self, device: DeviceT) -> None:
        if registered := self._registry_device(device):
            self._registry.async_update_device(
                registered.id, name=device.descriptor.name
            )

    @callback
    def _remove_registry_device(self, device: DeviceT) -> None:
        if registered := self._registry_device(device):
            self._registry.async_remove_device(registered.id)

    @callback
    def _device_removed(self, device: DeviceT) -> None:
        if coordinator := self.coordinators.pop(device.descriptor.dev_eui, None):
            self._entry.async_create_task(
                self._hass,
                coordinator.async_shutdown(),
                "Stop LoRaWAN device coordinator",
            )

    @callback
    def close(self) -> None:
        """Retire coordinators and subscriptions without deleting registered devices."""
        if self._closed:
            return
        self._closed = True
        self.collection.close()
        self._unsubscribe_added()
        self._unsubscribe_removed()
        self._listeners.clear()
