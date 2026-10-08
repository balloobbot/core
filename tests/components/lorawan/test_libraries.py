"""Exercise collection contracts and the real generated payload interface."""

# Direct imports preserve the vendored library boundary.
# pylint: disable=home-assistant-component-root-import

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

from lorawan_connection import (
    AddedEvent,
    Device,
    DeviceDescriptor,
    DeviceEvent,
    EventType,
    RemovedEvent,
    UpdatedEvent,
    UplinkEvent,
)
from lorawan_connection.mock import MockConnection
import pytest

from homeassistant.components.dragino._vendor.dragino_lorawan import DraginoDevices
from homeassistant.components.sensecap._vendor.sensecap_lorawan import (
    S2101,
    SenseCapDeviceCollection,
    decode_s2101,
)
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration, async_get_lorawan
from homeassistant.util import dt as dt_util

NOW = dt_util.utcnow()
DESCRIPTOR = DeviceDescriptor(
    "network",
    "0201010101010101",
    "Greenhouse",
    "application",
    "profile",
    S2101.identifiers["chirpstack"][1],
    744,
    stack="chirpstack",
)
PAYLOAD = bytes.fromhex("01011098530000010210A87A0000AF51")


def inventory(
    descriptor: DeviceDescriptor = DESCRIPTOR, kind: EventType = EventType.ADDED
) -> DeviceEvent:
    """Build an inventory fixture."""
    return {
        EventType.ADDED: AddedEvent,
        EventType.UPDATED: UpdatedEvent,
        EventType.REMOVED: RemovedEvent,
    }[kind](received_at=NOW, descriptor=descriptor)


def test_collection_lifecycle() -> None:
    """Initial replay, idempotence, independent devices and retirement."""
    collection = SenseCapDeviceCollection(MockConnection())
    collection.handle_event(inventory())
    added, removed = Mock(), Mock()
    stop = collection.subscribe_device_added(added)
    collection.subscribe_device_removed(removed)
    added.assert_called_once()
    model = added.call_args.args[0]
    collection.handle_event(inventory())
    collection.handle_event(
        inventory(replace(DESCRIPTOR, name="Renamed"), EventType.UPDATED)
    )
    added.assert_called_once()
    assert model.descriptor.name == "Renamed"
    collection.handle_event(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
    assert added.call_count == 2
    collection.handle_event(
        inventory(replace(DESCRIPTOR, model_id="unsupported"), EventType.UPDATED)
    )
    assert model.closed
    removed.assert_called_once_with(model)
    stop()
    stop()
    collection.handle_event(inventory())
    assert added.call_count == 2
    collection.close()
    collection.close()
    assert not collection.devices
    collection.handle_event(inventory())
    assert not collection.devices


def test_uplink_and_partial_state() -> None:
    """Typed uplinks preserve payload bytes and partial device updates."""
    collection = SenseCapDeviceCollection(MockConnection())
    collection.handle_event(inventory())
    device = collection.devices[DESCRIPTOR.dev_eui]
    listener = Mock()
    stop = device.add_update_listener(listener)
    event = UplinkEvent(
        network_id="network",
        dev_eui=DESCRIPTOR.dev_eui,
        received_at=NOW,
        data=PAYLOAD,
        f_port=1,
    )
    assert event.data is PAYLOAD
    collection.handle_event(event)
    assert device.temperature == 21.4
    assert device.humidity == 31.4
    listener.assert_called_once()
    collection.handle_event(event)
    assert listener.call_count == 2
    partial = bytes.fromhex("010110F0D8FFFF0000")
    collection.handle_event(
        replace(event, received_at=NOW + timedelta(seconds=1), data=partial)
    )
    assert device.temperature == -10
    assert device.humidity == 31.4
    collection.handle_event(event)
    assert device.temperature == -10
    stop()
    collection.close()


@pytest.mark.parametrize(
    "payload", [b"", b"\x01", PAYLOAD[:-1], bytes.fromhex("010210FFFFFFFF0000")]
)
def test_malformed_payload_atomic(payload: bytes) -> None:
    """Invalid frames cannot partially modify existing state."""
    with pytest.raises(ValueError):
        decode_s2101(payload)


def test_zero_unknown_fields_and_port() -> None:
    """Zero is meaningful; unknown record types and FPorts do not erase it."""
    assert decode_s2101(bytes.fromhex("01011000000000010210000000000000")) == {
        "temperature": 0,
        "humidity": 0,
    }
    assert decode_s2101(bytes.fromhex("010700640000000000")) == {}
    collection = SenseCapDeviceCollection(MockConnection())
    collection.handle_event(inventory())
    collection.handle_event(
        UplinkEvent(
            network_id="network",
            dev_eui=DESCRIPTOR.dev_eui,
            received_at=NOW,
            data=PAYLOAD,
            f_port=99,
        )
    )
    assert collection.devices[DESCRIPTOR.dev_eui].temperature is None


def test_unknown_device_network_and_vendor() -> None:
    """Payload, name, or wrong vendor cannot create an unrecognized model."""
    collection = SenseCapDeviceCollection(MockConnection())
    collection.handle_event(
        UplinkEvent(
            network_id="network",
            dev_eui=DESCRIPTOR.dev_eui,
            received_at=NOW,
            data=PAYLOAD,
        )
    )
    collection.handle_event(inventory(replace(DESCRIPTOR, network_id="other")))
    collection.handle_event(inventory(replace(DESCRIPTOR, brand_id=1)))
    assert not collection.devices


def test_listener_exception_and_unsubscribe() -> None:
    """One consumer cannot stop another receiving devices."""
    collection = SenseCapDeviceCollection(MockConnection())
    collection.subscribe_device_added(Mock(side_effect=ValueError))
    listener = Mock()
    collection.subscribe_device_added(listener)
    collection.handle_event(inventory())
    listener.assert_called_once()


@pytest.mark.parametrize("eui", ["xyz", "0" * 17, "g" * 16])
def test_invalid_eui(eui: str) -> None:
    """Reject invalid identities at the descriptor boundary."""
    with pytest.raises(ValueError):
        replace(DESCRIPTOR, dev_eui=eui)


def test_same_millisecond_partial_updates() -> None:
    """Different live readings may share a server receipt timestamp."""
    collection = SenseCapDeviceCollection(MockConnection())
    collection.handle_event(inventory())
    event = UplinkEvent(
        network_id="network", dev_eui=DESCRIPTOR.dev_eui, received_at=NOW, data=PAYLOAD
    )
    collection.handle_event(event)
    collection.handle_event(replace(event, data=bytes.fromhex("010110000000000000")))
    device = collection.devices[DESCRIPTOR.dev_eui]
    assert device.temperature == 0
    assert device.humidity == 31.4
    collection.close()


@pytest.mark.parametrize(
    ("domain", "brand_id", "models"),
    [
        ("sensecap", 744, SenseCapDeviceCollection.DEVICES),
        ("dragino", 676, DraginoDevices.DEVICES),
    ],
)
async def test_registered_models(
    hass: HomeAssistant, domain: str, brand_id: int, models: tuple[type[Device], ...]
) -> None:
    """Discovery metadata must match the models the vendor library can create."""
    registrations = await async_get_lorawan(hass)
    assert ("chirpstack", brand_id) in registrations[domain]
    integration = await async_get_integration(hass, domain)
    platform = await integration.async_get_platform("lorawan")
    assert models == platform.DEVICE_MODELS
    assert all(
        (stack, identity[0]) in registrations[domain]
        for model in models
        for stack, identity in model.identifiers.items()
    )
