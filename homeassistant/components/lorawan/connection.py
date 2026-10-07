"""Register active server connections and route their device events."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, override

from lorawan_connection import (
    Connection,
    ConnectionUnavailable,
    Device,
    DeviceCollection,
    DeviceDescriptor,
    DeviceEvent,
    Downlink,
    EventType,
    Unsubscribe,
    notify,
)

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant, callback as hass_callback
from homeassistant.helpers import discovery_flow
from homeassistant.util.hass_dict import HassKey


class ProviderConnection(Connection, Protocol):
    """A backend that can also subscribe to every device in its scope."""

    @override
    async def async_subscribe(
        self,
        *,
        brands: frozenset[tuple[str, int | str]] | None,
        callback: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        """Replay current devices, then deliver live events; None selects all brands."""


class _ConsumerConnection:
    """Expose device operations without transport lifecycle methods."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def async_subscribe(
        self,
        *,
        brands: frozenset[tuple[str, int | str]] | None,
        callback: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        return await self._connection.async_subscribe(brands=brands, callback=callback)

    def on_disconnect(self, callback: Callable[[], None]) -> Unsubscribe:
        return self._connection.on_disconnect(callback)

    async def async_send_downlink(self, downlink: Downlink) -> str:
        return await self._connection.async_send_downlink(downlink)


@dataclass
class RegisteredConnection:
    """An active connection and the current devices in its selected scope."""

    entry_id: str
    connection: Connection
    collection: DeviceCollection[Device]

    @property
    def devices(self) -> dict[str, DeviceDescriptor]:
        """Return the current inventory descriptors."""
        return {
            eui: device.descriptor for eui, device in self.collection.devices.items()
        }


@dataclass
class ConnectionRegistry:
    """Keep active registrations and transient subscription callbacks."""

    integrations: dict[str, list[tuple[str, int | str]]]
    connections: dict[str, RegisteredConnection] = field(default_factory=dict)
    inventories: dict[str, DeviceCollection[Device]] = field(default_factory=dict)
    changed: list[Callable[[str], None]] = field(default_factory=list)
    events: list[Callable[[tuple[str, DeviceEvent]], None]] = field(
        default_factory=list
    )


DATA_REGISTRY: HassKey[ConnectionRegistry] = HassKey("lorawan")


@hass_callback
def async_get_connections(hass: HomeAssistant) -> dict[str, Connection]:
    """Return the currently registered server connections by config entry ID."""
    return {
        entry_id: registered.connection
        for entry_id, registered in hass.data[DATA_REGISTRY].connections.items()
    }


async def async_register_connection(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    connection: ProviderConnection,
) -> Unsubscribe:
    """Register a connected server; return a callback that withdraws it.

    The provider owns transport setup, reconnection and shutdown. Registration
    reads the initial device list before making the connection available.
    """
    registry = hass.data[DATA_REGISTRY]
    if entry.entry_id in registry.connections:
        raise ValueError("A connection is already registered for this entry")
    consumer = _ConsumerConnection(connection)
    collection = DeviceCollection(consumer)
    registered = RegisteredConnection(entry.entry_id, consumer, collection)
    active = False
    disconnected = False

    @hass_callback
    def handle_event(event: DeviceEvent) -> None:
        if event.network_id != entry.entry_id:
            return
        collection.handle_event(event)
        if active:
            notify(registry.events, (entry.entry_id, event))
            if event.type != EventType.REMOVED:
                discover(event.descriptor)

    @hass_callback
    def discover(descriptor: DeviceDescriptor | None) -> None:
        if descriptor is None:
            return
        for domain, brands in registry.integrations.items():
            if (descriptor.stack, descriptor.brand_id) in brands:
                discovery_flow.async_create_flow(
                    hass,
                    domain,
                    context={"source": SOURCE_INTEGRATION_DISCOVERY},
                    data={},
                )

    @hass_callback
    def unregister() -> None:
        nonlocal active, disconnected
        disconnected = True
        if active:
            active = False
            registry.connections.pop(entry.entry_id, None)
            notify(registry.changed, entry.entry_id)
        for unsubscribe in unsubscribes:
            unsubscribe()
        unsubscribes.clear()

    unsubscribes: list[Unsubscribe] = []
    try:
        unsubscribes.append(connection.on_disconnect(unregister))
        unsubscribes.append(
            await connection.async_subscribe(brands=None, callback=handle_event)
        )
    except BaseException:
        unregister()
        collection.close()
        raise
    if disconnected:
        unregister()
        collection.close()
        raise ConnectionUnavailable("Connection lost during registration")
    if previous := registry.inventories.get(entry.entry_id):
        for eui, device in collection.devices.items():
            if (
                device.latest_status is None
                and (old_device := previous.devices.get(eui)) is not None
                and (status := old_device.latest_status) is not None
            ):
                collection.handle_event(status)
        previous.close()
    registry.inventories[entry.entry_id] = collection
    registry.connections[entry.entry_id] = registered
    active = True
    notify(registry.changed, entry.entry_id)
    for descriptor in tuple(registered.devices.values()):
        discover(descriptor)
    return unregister
