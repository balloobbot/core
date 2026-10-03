"""Exercise model-driven HA entity wiring and collection discovery."""

# Direct imports preserve the vendored library boundary.
# pylint: disable=home-assistant-component-root-import

from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import DeviceEventData, EventType, UplinkData
from lorawan_connection.chirpstack import ConnectionUnavailable

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
        data={"provider_entry_id": "provider", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    stop = Mock()

    async def subscribe(*, vendor_ids: frozenset[int], callback: Mock) -> Mock:
        callback(inventory())
        return stop

    connection = Mock(async_subscribe=AsyncMock(side_effect=subscribe))
    with patch(
        "homeassistant.components.sensecap.lorawan.get_connection",
        return_value=connection,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
        event = DeviceEventData(
            "network",
            DESCRIPTOR.dev_eui,
            EventType.UPLINK,
            dt_util.utcnow(),
            data=UplinkData(PAYLOAD),
        )
        entry.runtime_data.handle_event(event)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature").state == "21.4"
        assert hass.states.get("sensor.greenhouse_humidity").state == "31.4"
        second = replace(DESCRIPTOR, dev_eui="0201010101010102", name="Bedroom")
        entry.runtime_data.handle_event(inventory(second))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.bedroom_temperature").state == "unknown"
        temporary = replace(DESCRIPTOR, dev_eui="0201010101010103", name="Temporary")
        entry.runtime_data.handle_event(inventory(temporary))
        entry.runtime_data.handle_event(inventory(temporary, EventType.REMOVED))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.temporary_temperature") is None
        assert hass.states.get("sensor.temporary_humidity") is None
        entry.runtime_data.handle_event(inventory(DESCRIPTOR, EventType.REMOVED))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature") is None
        devices = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        stop.assert_called_once()
        assert not devices.devices


async def test_provider_unavailable(hass: HomeAssistant) -> None:
    """A disconnected provider causes setup retry at entry level."""
    entry = MockConfigEntry(
        domain="sensecap",
        data={"provider_entry_id": "missing", "network_id": "network"},
    )
    entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.sensecap.lorawan.get_connection",
        side_effect=ConnectionUnavailable,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state == ConfigEntryState.SETUP_RETRY


async def test_discovery(hass: HomeAssistant) -> None:
    """Discover one collection per provider, with explicit confirmation."""
    data = {"provider_entry_id": "provider", "network_id": "network"}
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
