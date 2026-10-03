"""Catalog discovery, real provider wiring, relay commands, and lifecycle."""

from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from lorawan_connection import (
    DeviceDescriptor,
    DeviceEventData,
    DownlinkError,
    EventType,
    UplinkData,
)
import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry

DESCRIPTOR = DeviceDescriptor(
    network_id="network",
    dev_eui="0201010101010102",
    name="Workshop",
    application_id="application",
    profile_id="profile",
    vendor_id=676,
    catalog_model_id="cb0a7bef-eaa0-4c61-a0b6-ce33e6ecbc4f",
)


def inventory(kind: EventType = EventType.ADDED) -> DeviceEventData:
    """Build a catalog inventory event."""
    return DeviceEventData(
        "network", DESCRIPTOR.dev_eui, kind, dt_util.utcnow(), DESCRIPTOR
    )


@pytest.fixture
async def setup_dragino(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> AsyncGenerator[tuple[MockConfigEntry, Callable]]:
    """Discover the vendor through provider inventory and confirm the collection."""
    mock_connection.async_send_downlink = AsyncMock(return_value="queue-id")
    mock_connection.async_subscribe.side_effect = lambda callback, disconnected: (
        callback(inventory())
    )
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    await hass.async_block_till_done()
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == "dragino"
    ]
    assert len(flows) == 1
    flow = flows[0]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    vendor = result["result"]
    await hass.async_block_till_done()
    yield vendor, mock_connection.async_subscribe.call_args.args[0]
    await hass.config_entries.async_unload(vendor.entry_id)
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_relay_cycle(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
    mock_connection: Mock,
) -> None:
    """An accepted command does not change state; its telemetry does."""
    _, emit = setup_dragino
    assert hass.states.get("switch.workshop_relay_1").state == "unknown"
    assert hass.states.get("switch.workshop_relay_2").state == "unknown"
    report = DeviceEventData(
        "network",
        DESCRIPTOR.dev_eui,
        EventType.UPLINK,
        dt_util.utcnow(),
        data=UplinkData(bytes(10) + b"\x41", 2),
    )
    emit(report)
    await hass.async_block_till_done()
    assert hass.states.get("switch.workshop_relay_1").state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.workshop_relay_1"}, blocking=True
    )
    request = mock_connection.async_send_downlink.call_args.args[0]
    assert request.dev_eui == DESCRIPTOR.dev_eui
    assert request.data == bytes.fromhex("030111")
    assert hass.states.get("switch.workshop_relay_1").state == "off"
    emit(replace(report, data=UplinkData(bytes(8) + bytes((0x80, 0, 0x41)), 2)))
    await hass.async_block_till_done()
    assert hass.states.get("switch.workshop_relay_1").state == "on"
    assert hass.states.get("switch.workshop_relay_2").state == "off"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.workshop_relay_2"}, blocking=True
    )
    assert mock_connection.async_send_downlink.call_args.args[0].data == bytes.fromhex(
        "031100"
    )
    emit(inventory(EventType.REMOVED))
    await hass.async_block_till_done()
    assert hass.states.get("switch.workshop_relay_1") is None
    assert hass.states.get("switch.workshop_relay_2") is None


@pytest.mark.usefixtures("setup_dragino")
async def test_read_only_key(
    hass: HomeAssistant,
    mock_connection: Mock,
) -> None:
    """Keep the switch visible and fail only the requested command."""
    mock_connection.async_send_downlink.side_effect = DownlinkError(
        "API key lacks write permission"
    )
    with pytest.raises(HomeAssistantError, match="write permission"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.workshop_relay_1"}, blocking=True
        )
    assert hass.states.get("switch.workshop_relay_1").state == "unknown"


async def test_unavailable_provider(hass: HomeAssistant) -> None:
    """Retry setup until its provider connects."""
    entry = MockConfigEntry(
        domain="dragino", data={"provider_entry_id": "missing", "network_id": "network"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_user_flow(hass: HomeAssistant) -> None:
    """Initial setup starts with the LoRaWAN provider."""
    result = await hass.config_entries.flow.async_init(
        "dragino", context={"source": SOURCE_USER}
    )
    assert result["reason"] == "discovery_only"
