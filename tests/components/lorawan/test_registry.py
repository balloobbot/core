"""Registry cleanup for live changes and devices removed while offline."""

# Direct imports preserve the vendored library boundary.
# pylint: disable=home-assistant-component-root-import

import asyncio
from dataclasses import replace
from unittest.mock import patch

from lorawan_connection import ConnectionUnavailable, EventType
from lorawan_connection.mock import MockConnection
import pytest

from homeassistant.components.dragino._vendor.dragino_lorawan import LT22222
from homeassistant.components.lorawan import LoRaWANEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry


@pytest.mark.parametrize("domain", ["sensecap", "dragino"])
async def test_registry_lifecycle(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    domain: str,
) -> None:
    """Delete registry entries on removal, but preserve them during an unload."""
    descriptor = (
        DESCRIPTOR
        if domain == "sensecap"
        else replace(
            DESCRIPTOR,
            vendor_id=LT22222.vendor_id,
            catalog_model_id=LT22222.catalog_model_id,
        )
    )
    entry = MockConfigEntry(
        domain=domain, data={"connection_entry_id": "provider", "network_id": "network"}
    )
    entry.add_to_hass(hass)
    connection = MockConnection([descriptor])
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(domain, "network:0000000000000001")},
    )
    stale_entity = entity_registry.async_get_or_create(
        "sensor",
        domain,
        "stale",
        config_entry=entry,
        device_id=stale.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    with patch(
        f"homeassistant.components.{domain}.get_connection", return_value=connection
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert device_registry.async_get(stale.id) is None
        assert entity_registry.async_get(stale_entity.entity_id) is None
        device = device_registry.async_get_device_by_identifier(
            (domain, f"network:{descriptor.dev_eui}"), entry.entry_id
        )
        assert device is not None
        connection.emit(
            inventory(replace(descriptor, name="Renamed"), EventType.UPDATED)
        )
        assert device_registry.async_get(device.id).name == "Renamed"
        # Unload is not a device deletion.
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert device_registry.async_get(device.id) is not None
        # The device disappeared while HA was offline.
        connection.emit(inventory(descriptor, EventType.REMOVED))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert device_registry.async_get(device.id) is None
        assert not er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        connection.emit(inventory(descriptor))
        await hass.async_block_till_done()
        device = device_registry.async_get_device_by_identifier(
            (domain, f"network:{descriptor.dev_eui}"), entry.entry_id
        )
        assert device is not None
        # A profile change to an unsupported model removes all its entities too.
        connection.emit(
            inventory(
                replace(descriptor, catalog_model_id="unsupported"), EventType.UPDATED
            )
        )
        await hass.async_block_till_done()
        assert device_registry.async_get(device.id) is None
        assert not er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("domain", ["sensecap", "dragino"])
async def test_subscription_failure_preserves_registry(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, domain: str
) -> None:
    """An unavailable snapshot cannot establish that a device was removed."""
    entry = MockConfigEntry(
        domain=domain, data={"connection_entry_id": "provider", "network_id": "network"}
    )
    entry.add_to_hass(hass)
    existing = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(domain, "network:0000000000000001")},
    )
    connection = MockConnection()
    with (
        patch(
            f"homeassistant.components.{domain}.get_connection", return_value=connection
        ),
        patch.object(
            connection,
            "async_subscribe",
            side_effect=ConnectionUnavailable("Disconnected before subscription"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert device_registry.async_get(existing.id) is not None


async def test_removal_during_entity_add(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Removal while async_added_to_hass is suspended leaves no orphan entities."""
    entry = MockConfigEntry(
        domain="sensecap",
        data={"connection_entry_id": "provider", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    connection = MockConnection([DESCRIPTOR])
    entered, finish = asyncio.Event(), asyncio.Event()
    original = LoRaWANEntity.async_added_to_hass

    async def delayed(entity: LoRaWANEntity) -> None:
        entered.set()
        await finish.wait()
        await original(entity)

    with (
        patch(
            "homeassistant.components.sensecap.get_connection", return_value=connection
        ),
        patch.object(LoRaWANEntity, "async_added_to_hass", delayed),
    ):
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        await entered.wait()
        connection.emit(inventory(DESCRIPTOR, EventType.REMOVED))
        finish.set()
        assert await setup
        await hass.async_block_till_done()
    assert not dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert not er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert hass.states.get("sensor.greenhouse_temperature") is None
    assert hass.states.get("sensor.greenhouse_humidity") is None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("domain", ["sensecap", "dragino"])
@pytest.mark.parametrize("entry_key", ["provider_entry_id", "connection_entry_id"])
async def test_connection_entry_migration(
    hass: HomeAssistant, domain: str, entry_key: str
) -> None:
    """Existing collections retain their connection and network when renamed."""
    entry = MockConfigEntry(
        domain=domain,
        version=1,
        minor_version=1,
        unique_id="network",
        data={entry_key: "connection", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    with patch(
        f"homeassistant.components.{domain}.get_connection",
        return_value=MockConnection(),
    ) as get_connection:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    get_connection.assert_called_once_with(hass, "connection")
    assert entry.data == {
        "connection_entry_id": "connection",
        "network_id": "network",
    }
    assert (entry.version, entry.minor_version) == (1, 2)
    assert entry.unique_id == "network"
    assert await hass.config_entries.async_unload(entry.entry_id)
