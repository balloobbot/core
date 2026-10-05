"""Shared lifecycle with ordinary coordinators and standard device identities."""

# Direct imports preserve the vendored library boundary.
# pylint: disable=home-assistant-component-root-import

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import replace
import logging
from unittest.mock import patch

from lorawan_connection import (
    Connection,
    ConnectionUnavailable,
    DeviceEvent,
    EventType,
    Unsubscribe,
)
from lorawan_connection.mock import MockConnection
import pytest

from homeassistant.components.lorawan import DeviceManager, device_identifier
from homeassistant.components.sensecap._vendor.sensecap_lorawan import (
    S2101,
    SenseCapDeviceCollection,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry

type Manager = DeviceManager[S2101, DataUpdateCoordinator[S2101]]
_LOGGER = logging.getLogger(__name__)


def create_coordinator(
    hass: HomeAssistant, device: S2101
) -> DataUpdateCoordinator[S2101]:
    """Use a normal coordinator with no device_info property or special base."""
    coordinator = DataUpdateCoordinator[S2101](
        hass, _LOGGER, config_entry=None, name=device.descriptor.name
    )
    coordinator.async_set_updated_data(device)
    return coordinator


@pytest.fixture
def connection() -> Generator[MockConnection]:
    """Supply a device without opening a transport."""
    connection = MockConnection([DESCRIPTOR])
    with patch(
        "homeassistant.components.lorawan.device_manager._async_get_connection",
        return_value=connection,
    ):
        yield connection


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Own only this collection's registry records."""
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def manager(
    hass: HomeAssistant, entry: MockConfigEntry, connection: MockConnection
) -> AsyncGenerator[Manager]:
    """Close the collection and await coordinator retirement after each test."""
    manager = DeviceManager(
        hass,
        entry,
        create_collection=SenseCapDeviceCollection,
        create_coordinator=create_coordinator,
    )
    yield manager
    manager.close()
    await hass.async_block_till_done()


async def test_ready_coordinators(
    hass: HomeAssistant, manager: Manager, connection: MockConnection
) -> None:
    """Subscribers receive initialized coordinators for existing and new devices."""
    first: list[DataUpdateCoordinator[S2101]] = []
    unsubscribe = manager.subscribe_coordinator_added(first.append)
    await manager.async_setup(connection_entry_id="selected-connection")
    assert first[0].data.descriptor == DESCRIPTOR
    assert first[0] is manager.coordinators[DESCRIPTOR.dev_eui]
    second: list[DataUpdateCoordinator[S2101]] = []
    manager.subscribe_coordinator_added(second.append)
    assert second == first

    descriptor = replace(DESCRIPTOR, dev_eui="0201010101010102")
    connection.emit(inventory(descriptor))
    assert first == second
    assert first[1].data.descriptor == descriptor
    unsubscribe()
    unsubscribe()
    connection.emit(inventory(replace(DESCRIPTOR, dev_eui="0201010101010103")))
    assert len(first) == 2
    assert len(second) == 3

    with patch.object(
        first[0], "async_shutdown", wraps=first[0].async_shutdown
    ) as stop:
        manager.close()
        manager.close()
        await hass.async_block_till_done()
    stop.assert_awaited_once()
    assert manager.coordinators == {}
    assert all(coordinator.data.closed for coordinator in second)
    with pytest.raises(RuntimeError, match="closed"):
        manager.subscribe_coordinator_added(first.append)


async def test_registry_scope_without_device_info(
    hass: HomeAssistant,
    manager: Manager,
    connection: MockConnection,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Identity alone supports cleanup without touching another entry's device."""
    other = MockConfigEntry(domain="sensecap")
    other.add_to_hass(hass)
    device = S2101(DESCRIPTOR)
    identifiers = {device_identifier("sensecap", device)}
    owned = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=identifiers
    )
    foreign = device_registry.async_get_or_create(
        config_entry_id=other.entry_id, identifiers=identifiers
    )
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sensecap", "network:missing")}
    )
    await manager.async_setup(connection_entry_id="selected-connection")
    assert device_registry.async_get(stale.id) is None
    assert device_registry.async_get(owned.id) is not None
    connection.emit(inventory(replace(DESCRIPTOR, name="New name"), EventType.UPDATED))
    assert device_registry.async_get(owned.id).name == "New name"
    connection.emit(inventory(DESCRIPTOR, EventType.REMOVED))
    await hass.async_block_till_done()
    assert device_registry.async_get(owned.id) is None
    assert device_registry.async_get(foreign.id) is not None
    assert manager.coordinators == {}


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (ConnectionUnavailable, ConfigEntryNotReady),
        (asyncio.CancelledError, asyncio.CancelledError),
    ],
)
async def test_partial_subscription_failure(
    hass: HomeAssistant,
    manager: Manager,
    connection: MockConnection,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    error: type[BaseException],
    expected_error: type[BaseException],
) -> None:
    """Retire partial setup without treating missing devices as remote deletions."""
    registered = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sensecap", "network:missing")}
    )
    delivered: list[DataUpdateCoordinator[S2101]] = []
    manager.subscribe_coordinator_added(delivered.append)

    async def fail_after_device(
        *, vendor_ids: frozenset[int], callback: Callable[[DeviceEvent], None]
    ) -> Unsubscribe:
        callback(inventory(DESCRIPTOR))
        raise error

    with (
        patch.object(connection, "async_subscribe", new=fail_after_device),
        pytest.raises(expected_error),
    ):
        await manager.async_setup(connection_entry_id="selected-connection")
    await hass.async_block_till_done()
    assert len(delivered) == 1
    assert delivered[0].data.closed
    assert not manager.coordinators
    assert device_registry.async_get(registered.id) is not None
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        connection.disconnect()
    reload.assert_not_called()


@pytest.mark.usefixtures("connection")
async def test_coordinator_failure_preserves_registry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A failed coordinator must not make setup reconcile a partial collection."""
    registered = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sensecap", "network:missing")}
    )

    def fail(hass: HomeAssistant, device: S2101) -> DataUpdateCoordinator[S2101]:
        raise ValueError("Cannot create coordinator")

    manager = DeviceManager(
        hass,
        entry,
        create_collection=SenseCapDeviceCollection,
        create_coordinator=fail,
    )
    with pytest.raises(HomeAssistantError, match="Failed to create"):
        await manager.async_setup(connection_entry_id="selected-connection")
    assert not manager.collection.devices
    assert device_registry.async_get(registered.id) is not None


async def test_failing_platform_listener(
    manager: Manager, connection: MockConnection
) -> None:
    """One failing platform must not stop other platforms receiving a new device."""

    def fail(coordinator: DataUpdateCoordinator[S2101]) -> None:
        raise ValueError("Cannot create entity")

    manager.subscribe_coordinator_added(fail)
    await manager.async_setup(connection_entry_id="selected-connection")
    delivered: list[DataUpdateCoordinator[S2101]] = []
    manager.subscribe_coordinator_added(delivered.append)
    connection.emit(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
    assert len(delivered) == 2


async def test_failed_replay_unsubscribes(
    manager: Manager, connection: MockConnection
) -> None:
    """A platform whose initial replay fails must not leave a listener behind."""
    await manager.async_setup(connection_entry_id="selected-connection")

    calls: list[DataUpdateCoordinator[S2101]] = []

    def fail(coordinator: DataUpdateCoordinator[S2101]) -> None:
        calls.append(coordinator)
        raise ValueError("Cannot create entity")

    with pytest.raises(ValueError, match="Cannot create entity"):
        manager.subscribe_coordinator_added(fail)
    connection.emit(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
    assert len(calls) == 1


async def test_explicit_connection_selection(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: Manager,
    connection: MockConnection,
) -> None:
    """Setup uses the explicit selection rather than the consuming entry's storage."""
    hass.config_entries.async_update_entry(
        entry, data={"connection_entry_id": "different-stored-entry"}
    )
    with patch(
        "homeassistant.components.lorawan.device_manager._async_get_connection",
        return_value=connection,
    ) as resolve:
        await manager.async_setup(connection_entry_id="explicit-entry")
    resolve.assert_called_once_with(hass, "explicit-entry")
    assert manager.collection.devices[DESCRIPTOR.dev_eui].descriptor == DESCRIPTOR
    with pytest.raises(RuntimeError, match="already set up"):
        await manager.async_setup(connection_entry_id="another-entry")


async def test_disconnect_reloads_consumer(
    hass: HomeAssistant,
    manager: Manager,
    entry: MockConfigEntry,
    connection: MockConnection,
) -> None:
    """The manager owns the consuming entry's reload listener."""
    await manager.async_setup(connection_entry_id="selected-connection")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        connection.disconnect()
    reload.assert_called_once_with(entry.entry_id)


async def test_close_releases_disconnect_listener(
    hass: HomeAssistant, manager: Manager, connection: MockConnection
) -> None:
    """Closing the manager leaves the connection usable and removes reload handling."""
    await manager.async_setup(connection_entry_id="selected-connection")
    manager.close()
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        connection.emit(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
        connection.disconnect()
    reload.assert_not_called()


async def test_shutdown_does_not_reload(
    hass: HomeAssistant, manager: Manager, connection: MockConnection
) -> None:
    """Connection loss while HA stops does not schedule a consuming entry reload."""
    await manager.async_setup(connection_entry_id="selected-connection")
    hass.set_state(CoreState.stopping)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        connection.disconnect()
    reload.assert_not_called()


async def test_collection_factory_failure(
    hass: HomeAssistant, entry: MockConfigEntry, connection: MockConnection
) -> None:
    """A failed factory releases the disconnect listener even without a collection."""

    def fail(connection: Connection) -> SenseCapDeviceCollection:
        raise ValueError("Cannot create collection")

    manager = DeviceManager(
        hass, entry, create_collection=fail, create_coordinator=create_coordinator
    )
    with pytest.raises(ValueError, match="Cannot create collection"):
        await manager.async_setup(connection_entry_id="selected-connection")
    manager.close()
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        connection.disconnect()
    reload.assert_not_called()


async def test_close_before_setup(manager: Manager) -> None:
    """Unload remains safe before a connection has been selected."""
    manager.close()
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        await manager.async_setup(connection_entry_id="selected-connection")
