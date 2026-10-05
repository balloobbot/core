"""Multi-server lifecycle and isolated recovery for ordinary coordinators."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from lorawan_connection import ConnectionUnavailable, DownlinkError
import pytest

from homeassistant.components.lorawan import async_get_connections
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import RegisterBackend
from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry


async def test_multiple_connections_reconnect(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Same EUI on two connections stays independent; reconnect reuses coordinators."""
    first, unregister = await registered_backend("network", [DESCRIPTOR])
    other = replace(DESCRIPTOR, network_id="other", name="Bedroom")
    _second, _ = await registered_backend("other", [other])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data
    coordinator = manager.coordinators[("network", DESCRIPTOR.dev_eui)]
    model = coordinator.data
    assert len(manager.coordinators) == 2
    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 2
    before = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        first._failed(ConnectionUnavailable())
        await hass.async_block_till_done()
    reload.assert_not_called()
    assert hass.states.get("sensor.greenhouse_temperature").state == "unavailable"
    assert hass.states.get("sensor.bedroom_temperature").state == "unknown"
    assert entry.state is ConfigEntryState.LOADED
    assert not model.closed
    assert "network" not in async_get_connections(hass)
    unregister()
    first, _ = await registered_backend("network", [DESCRIPTOR])
    await hass.async_block_till_done()
    assert manager.coordinators[("network", DESCRIPTOR.dev_eui)] is coordinator
    assert coordinator.data is model
    assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
    assert er.async_entries_for_config_entry(entity_registry, entry.entry_id) == before
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert model.closed


async def test_late_connections(
    hass: HomeAssistant, registered_backend: RegisterBackend
) -> None:
    """A vendor starts without servers and consumes future registrations."""
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not entry.runtime_data.coordinators
    await registered_backend("network", [DESCRIPTOR])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.greenhouse_temperature") is not None
    assert len(entry.runtime_data.coordinators) == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_offline_removals(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Reconnect removes missing devices only from that connection."""
    _, unregister = await registered_backend("network", [DESCRIPTOR])
    other = replace(DESCRIPTOR, network_id="other", name="Bedroom")
    await registered_backend("other", [other])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    unregister()
    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 2
    await registered_backend("network", [])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.greenhouse_temperature") is None
    assert hass.states.get("sensor.bedroom_temperature") is not None
    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 1
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_deleted_server(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Deleting a config entry cleans up its devices even after disconnect."""
    _, unregister = await registered_backend("network", [DESCRIPTOR])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    unregister()
    await hass.config_entries.async_remove("network")
    await hass.async_block_till_done()
    assert not dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert not entry.runtime_data.coordinators
    assert hass.states.get("sensor.greenhouse_temperature") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_startup_cleanup(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Startup keeps offline-server devices and removes records for deleted servers."""
    await registered_backend("network", [])
    MockConfigEntry(domain="chirpstack", entry_id="offline").add_to_hass(hass)
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    missing = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("sensecap", "deleted:0000000000000001")},
    )
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("sensecap", "network:0000000000000001")},
    )
    offline = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("sensecap", "offline:0000000000000001")},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert device_registry.async_get(missing.id) is None
    assert device_registry.async_get(stale.id) is None
    assert device_registry.async_get(offline.id) is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_coordinator_listener_cleanup(
    hass: HomeAssistant, registered_backend
) -> None:
    """Subscriptions replay ready coordinators and unsubscribe independently."""
    backend, _ = await registered_backend("network", [DESCRIPTOR])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    delivered = []
    unsubscribe = entry.runtime_data.subscribe_coordinator_added(delivered.append)
    assert delivered[0].data.descriptor == DESCRIPTOR
    unsubscribe()
    unsubscribe()
    backend._emit(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
    assert len(delivered) == 1
    manager = entry.runtime_data
    await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(RuntimeError, match="closed"):
        manager.subscribe_coordinator_added(delivered.append)


async def test_reconnect_routes_commands_to_new_transport(
    hass: HomeAssistant, registered_backend: RegisterBackend
) -> None:
    """A retained model sends subsequent commands through the replacement backend."""

    first, unregister = await registered_backend("network", [DESCRIPTOR])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    device = entry.runtime_data.coordinators[("network", DESCRIPTOR.dev_eui)].data
    with patch.object(
        first, "async_send_downlink", new=AsyncMock(return_value="one")
    ) as send:
        await device.async_send_downlink(data=b"first", f_port=2, wait_for_ack=False)
        send.assert_awaited_once()
        unregister()
        second, _ = await registered_backend("network", [DESCRIPTOR])
        await hass.async_block_till_done()
        with patch.object(
            second, "async_send_downlink", new=AsyncMock(return_value="two")
        ) as replacement:
            await device.async_send_downlink(
                data=b"second", f_port=2, wait_for_ack=False
            )
            replacement.assert_awaited_once()
        send.assert_awaited_once()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_pending_command_fails_on_disconnect(
    hass: HomeAssistant, registered_backend: RegisterBackend
) -> None:
    """Disconnect fails ACK waits while preserving models and entity registrations."""

    backend, unregister = await registered_backend("network", [DESCRIPTOR])
    entry = MockConfigEntry(domain="sensecap")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    device = entry.runtime_data.coordinators[("network", DESCRIPTOR.dev_eui)].data
    with patch.object(
        backend, "async_send_downlink", new=AsyncMock(return_value="one")
    ):
        command = hass.async_create_task(
            device.async_send_downlink(data=b"command", f_port=2)
        )
        await asyncio.sleep(0)
        unregister()
        with pytest.raises(DownlinkError, match="Connection was lost"):
            await command
    assert not device.closed
    await hass.config_entries.async_unload(entry.entry_id)
