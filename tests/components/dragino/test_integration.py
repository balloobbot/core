"""Catalog discovery, real provider wiring, relay commands, and lifecycle."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import (
    AckData,
    DeviceDescriptor,
    DeviceEventData,
    DownlinkError,
    EventType,
    UplinkData,
)
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.config_entries import ConfigEntryState
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
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
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
    emit = mock_connection._emit

    async def send(downlink: object) -> str:
        emit(
            DeviceEventData(
                "network",
                DESCRIPTOR.dev_eui,
                EventType.ACK,
                dt_util.utcnow(),
                data=AckData("queue-id", True),
            )
        )
        return "queue-id"

    mock_connection.async_send_downlink.side_effect = send
    yield vendor, mock_connection._emit
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
    assert request.confirmed
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


async def test_command_waits_for_ack(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
    mock_connection: Mock,
) -> None:
    """Reports update the model while the service waits for device acknowledgement."""
    _, emit = setup_dragino
    sent = asyncio.Event()

    async def send(downlink: object) -> str:
        sent.set()
        return "queue-id"

    mock_connection.async_send_downlink.side_effect = send
    command = hass.async_create_task(
        hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.workshop_relay_1"}, blocking=True
        )
    )
    await sent.wait()
    assert not command.done()
    emit(
        DeviceEventData(
            "network",
            DESCRIPTOR.dev_eui,
            EventType.UPLINK,
            dt_util.utcnow(),
            data=UplinkData(bytes(8) + bytes((0x80, 0, 0x41)), 2),
        )
    )
    assert hass.states.get("switch.workshop_relay_1").state == "on"
    assert not command.done()
    emit(
        DeviceEventData(
            "network",
            DESCRIPTOR.dev_eui,
            EventType.ACK,
            dt_util.utcnow(),
            data=AckData("queue-id", True),
        )
    )
    await command


async def test_command_negative_ack(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
    mock_connection: Mock,
) -> None:
    """A failed acknowledgement reaches the service caller."""
    _, emit = setup_dragino

    async def send(downlink: object) -> str:
        emit(
            DeviceEventData(
                "network",
                DESCRIPTOR.dev_eui,
                EventType.ACK,
                dt_util.utcnow(),
                data=AckData("queue-id", False),
            )
        )
        return "queue-id"

    mock_connection.async_send_downlink.side_effect = send
    with pytest.raises(HomeAssistantError, match="did not acknowledge"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.workshop_relay_1"}, blocking=True
        )
    assert hass.states.get("switch.workshop_relay_1").state == "unknown"


@pytest.mark.usefixtures("setup_dragino")
async def test_command_timeout(hass: HomeAssistant, mock_connection: Mock) -> None:
    """The integration applies its deadline with the standard timeout context."""
    mock_connection.async_send_downlink.side_effect = None
    with (
        patch(
            "homeassistant.components.dragino.switch.timeout",
            return_value=asyncio.timeout(0),
        ),
        pytest.raises(HomeAssistantError, match="Timed out"),
    ):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.workshop_relay_1"}, blocking=True
        )


async def test_removed_while_entities_are_added(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
) -> None:
    """Retire entities even if their platform has not finished adding them."""
    _, emit = setup_dragino
    original_entities = set(hass.states.async_entity_ids())
    descriptor = replace(DESCRIPTOR, dev_eui="0201010101010103", name="Second")
    added = replace(inventory(), dev_eui=descriptor.dev_eui, descriptor=descriptor)
    emit(added)
    emit(replace(added, type=EventType.REMOVED))
    await hass.async_block_till_done()
    assert set(hass.states.async_entity_ids()) == original_entities


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("04ab04ac13101300890041", id="analog"),
        pytest.param("ffffffff80000001c30042", id="digital_counts"),
        pytest.param("8000000113101300020043", id="count_and_current"),
        pytest.param("80000001ffffffff400044", id="voltage_count"),
        pytest.param("04ab04ac1310ffff800045", id="mixed"),
    ],
)
async def test_input_entities(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
    snapshot: SnapshotAssertion,
    payload: str,
) -> None:
    """All channels map correctly and mode changes clear inapplicable readings."""
    _, emit = setup_dragino
    report = DeviceEventData(
        "network",
        DESCRIPTOR.dev_eui,
        EventType.UPLINK,
        dt_util.utcnow(),
        data=UplinkData(bytes.fromhex("04ab04ac13101300df0041"), 2),
    )
    emit(report)
    emit(replace(report, data=UplinkData(bytes.fromhex(payload), 2)))
    await hass.async_block_till_done()
    assert {
        state.entity_id: state.state for state in hass.states.async_all()
    } == snapshot
    assert (
        hass.states.get("sensor.workshop_voltage_1").attributes["unit_of_measurement"]
        == "V"
    )
    assert (
        hass.states.get("sensor.workshop_current_1").attributes["unit_of_measurement"]
        == "mA"
    )
    emit(inventory(EventType.REMOVED))
    await hass.async_block_till_done()
    assert not hass.states.async_all()


@pytest.mark.parametrize(
    ("entity_id", "action", "payload", "report", "expected"),
    [
        pytest.param(
            "switch.workshop_digital_output_1",
            "turn_on",
            "02011111",
            1,
            "on",
            id="do1_on",
        ),
        pytest.param(
            "switch.workshop_digital_output_1",
            "turn_off",
            "02001111",
            0,
            "off",
            id="do1_off",
        ),
        pytest.param(
            "switch.workshop_digital_output_2",
            "turn_on",
            "02110111",
            2,
            "on",
            id="do2_on",
        ),
        pytest.param(
            "switch.workshop_digital_output_2",
            "turn_off",
            "02110011",
            0,
            "off",
            id="do2_off",
        ),
    ],
)
async def test_digital_output_commands(
    hass: HomeAssistant,
    setup_dragino: tuple[MockConfigEntry, Callable],
    mock_connection: Mock,
    entity_id: str,
    action: str,
    payload: str,
    report: int,
    expected: str,
) -> None:
    """Active-low output commands wait for ACK and state comes from telemetry."""
    _, emit = setup_dragino
    assert hass.states.get(entity_id).state == "unknown"
    await hass.services.async_call(
        "switch", action, {"entity_id": entity_id}, blocking=True
    )
    request = mock_connection.async_send_downlink.call_args.args[0]
    assert request.confirmed
    assert request.data.hex() == payload
    assert hass.states.get(entity_id).state == "unknown"
    emit(
        DeviceEventData(
            "network",
            DESCRIPTOR.dev_eui,
            EventType.UPLINK,
            dt_util.utcnow(),
            data=UplinkData(bytes(8) + bytes((report, 0, 0x41)), 2),
        )
    )
    assert hass.states.get(entity_id).state == expected
