"""Manage vendor device collections and their Home Assistant coordinators."""

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from lorawan_connection import (
    Connection,
    ConnectionUnavailable,
    Device,
    DeviceCollection,
    Unsubscribe,
)

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from . import LoRaWANConfigEntry

_LOGGER = logging.getLogger(__name__)


class ConnectionNotFound(Exception):
    """The selected LoRaWAN connection entry does not exist."""


@callback
def _async_get_connection(hass: HomeAssistant, connection_entry_id: str) -> Connection:
    """Resolve the connection selected for this device manager."""
    entry: LoRaWANConfigEntry | None = hass.config_entries.async_get_entry(
        connection_entry_id
    )
    if entry is None or entry.domain != DOMAIN:
        raise ConnectionNotFound("The selected LoRaWAN connection entry was removed")
    if (
        entry.state is not ConfigEntryState.LOADED
        or not entry.runtime_data.connection.available
    ):
        raise ConnectionUnavailable("LoRaWAN connection is not available")
    return entry.runtime_data.consumer


def device_identifier(domain: str, device: Device) -> tuple[str, str]:
    """Identify a physical device within its vendor integration and network."""
    descriptor = device.descriptor
    return (domain, f"{descriptor.network_id}:{descriptor.dev_eui}")


class DeviceManager[DeviceT: Device, CoordinatorT: DataUpdateCoordinator[Any]]:
    """Own collection subscriptions, coordinators, and registry cleanup."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        create_collection: Callable[[Connection], DeviceCollection[DeviceT]],
        create_coordinator: Callable[[HomeAssistant, DeviceT], CoordinatorT],
    ) -> None:
        """Store the owner and factories without selecting a connection."""
        self._collection: DeviceCollection[DeviceT] | None = None
        self._create_collection = create_collection
        self.coordinators: dict[str, CoordinatorT] = {}
        self._hass = hass
        self._entry = entry
        self._registry = dr.async_get(hass)
        self._create_coordinator = create_coordinator
        self._listeners: list[Callable[[CoordinatorT], None]] = []
        self._closed = False
        self._unsubscribes: list[Unsubscribe] = []

    @property
    def collection(self) -> DeviceCollection[DeviceT]:
        """Return the collection created during setup."""
        if self._collection is None:
            raise RuntimeError("Device manager has not created its collection")
        return self._collection

    async def async_setup(self, *, connection_entry_id: str) -> None:
        """Resolve a connection, subscribe to devices, and reconcile registry records."""
        if self._closed or self._collection is not None:
            raise RuntimeError("Device manager is closed or already set up")
        try:
            connection = _async_get_connection(self._hass, connection_entry_id)
            self._unsubscribes.append(connection.on_disconnect(self._disconnected))
            self._collection = self._create_collection(connection)
            self._unsubscribes.append(
                self.collection.subscribe_device_removed(self._device_removed)
            )
            self._unsubscribes.append(
                self.collection.subscribe_device_added(self._device_added)
            )
            await self.collection.async_setup()
        except BaseException:
            self.close()
            raise
        # Collection callbacks isolate failures; do not reconcile a partial setup.
        if self.coordinators.keys() != self.collection.devices.keys():
            self.close()
            raise HomeAssistantError("Failed to create a LoRaWAN device coordinator")
        current = {
            device_identifier(self._entry.domain, device)
            for device in self.collection.devices.values()
        }
        for registered in dr.async_entries_for_config_entry(
            self._registry, self._entry.entry_id
        ):
            if not registered.identifiers.intersection(current):
                self._registry.async_remove_device(registered.id)

    @callback
    def _disconnected(self) -> None:
        if not self._hass.is_stopping:
            self._hass.config_entries.async_schedule_reload(self._entry.entry_id)

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
        return self._registry.async_get_device_by_identifier(
            device_identifier(self._entry.domain, device), self._entry.entry_id
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
        if self._collection is not None:
            self._collection.close()
        for unsubscribe in reversed(self._unsubscribes):
            unsubscribe()
        self._unsubscribes.clear()
        self._listeners.clear()
