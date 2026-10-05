"""Exercise model-driven HA entity wiring and collection discovery."""

# Direct imports preserve the vendored library boundary.
# pylint: disable=home-assistant-component-root-import

from dataclasses import replace
import gc
from unittest.mock import patch
import weakref

from lorawan_connection import DeviceEventData, EventType, UplinkData
from lorawan_connection.chirpstack import ConnectionUnavailable
from lorawan_connection.mock import MockConnection

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry
from tests.components.lorawan.test_libraries import DESCRIPTOR, PAYLOAD, inventory


async def test_sensor_lifecycle(hass: HomeAssistant) -> None:
    """One collection exposes two devices, merges state, and cleans up."""
    entry = MockConfigEntry(
        domain="sensecap",
        title="SenseCAP",
        data={"connection_entry_id": "provider", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    connection = MockConnection([DESCRIPTOR])
    with patch(
        "homeassistant.components.sensecap.async_get_connection",
        return_value=connection,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
        event = DeviceEventData(
            network_id="network",
            dev_eui=DESCRIPTOR.dev_eui,
            type=EventType.UPLINK,
            received_at=dt_util.utcnow(),
            data=UplinkData(PAYLOAD),
        )
        connection.emit(event)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature").state == "21.4"
        assert hass.states.get("sensor.greenhouse_humidity").state == "31.4"
        second = replace(DESCRIPTOR, dev_eui="0201010101010102", name="Bedroom")
        connection.emit(inventory(second))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.bedroom_temperature").state == "unknown"
        temporary = replace(DESCRIPTOR, dev_eui="0201010101010103", name="Temporary")
        connection.emit(inventory(temporary))
        connection.emit(inventory(temporary, EventType.REMOVED))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.temporary_temperature") is None
        assert hass.states.get("sensor.temporary_humidity") is None
        reference = weakref.ref(entry.runtime_data.coordinators[DESCRIPTOR.dev_eui])
        connection.emit(inventory(DESCRIPTOR, EventType.REMOVED))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature") is None
        gc.collect()
        assert reference() is None
        devices = entry.runtime_data.collection
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not devices.devices


async def test_provider_unavailable(hass: HomeAssistant) -> None:
    """A disconnected provider causes setup retry at entry level."""
    entry = MockConfigEntry(
        domain="sensecap",
        data={"connection_entry_id": "missing", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.sensecap.async_get_connection",
        side_effect=ConnectionUnavailable,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state == ConfigEntryState.SETUP_RETRY


async def test_discovery(hass: HomeAssistant) -> None:
    """Discover one collection per provider, with explicit confirmation."""
    data = {"connection_entry_id": "provider", "network_id": "network"}
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=data
    )
    assert result["step_id"] == "confirm"
    with patch(
        "homeassistant.components.sensecap.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=data
    )
    assert result["reason"] == "already_configured"
