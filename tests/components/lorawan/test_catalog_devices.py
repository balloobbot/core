"""Exercise catalog identity and telemetry through the vendor integrations."""

# pylint: disable=home-assistant-component-root-import

from dataclasses import replace
from datetime import timedelta

from lorawan_connection import Device, DeviceDescriptor, RemovedEvent, UplinkEvent
import pytest

from homeassistant.components.dragino._vendor.dragino_lorawan import LHT65
from homeassistant.components.milesight._vendor.milesight_lorawan import TS201, UC51x
from homeassistant.components.sensecap._vendor.sensecap_lorawan import S2101, S2102
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import RegisterBackend

from tests.common import MockConfigEntry


def descriptor(model: type[Device], stack: str = "tts") -> DeviceDescriptor:
    """Use the reviewed catalog identity rather than a deployment profile alias."""
    brand_id, model_id = model.identifiers[stack]
    return DeviceDescriptor(
        network_id="network",
        dev_eui="0201010101010101",
        stack=stack,
        name="Catalog device",
        application_id="application",
        profile_id="private-profile",
        brand_id=brand_id,
        model_id=model_id,
    )


@pytest.mark.parametrize(
    ("domain", "model", "stack", "port", "payload", "expected"),
    [
        pytest.param(
            "sensecap",
            S2102,
            "chirpstack",
            2,
            "010310a8490400059d",
            {"illuminance": "281.0"},
            id="chirpstack-s2102",
        ),
        pytest.param(
            "sensecap",
            S2102,
            "tts",
            2,
            "010310a8490400059d",
            {"illuminance": "281.0"},
            id="tts-s2102",
        ),
        pytest.param(
            "sensecap",
            S2101,
            "tts",
            2,
            "010110645f0000010210305b01001ced",
            {"temperature": "24.42", "humidity": "88.88"},
            id="sensecap-fport-two",
        ),
        pytest.param(
            "milesight",
            TS201,
            "tts",
            85,
            "01756103672201",
            {"temperature": "29.0", "battery": "97"},
            id="ts201-live-payload",
        ),
        pytest.param(
            "milesight",
            UC51x,
            "tts",
            85,
            "01755c03010004c80500000005010106c808000000",
            {
                "battery": "92",
                "valve_1": "closed",
                "valve_2": "open",
                "pulse_count_1": "5",
                "pulse_count_2": "8",
            },
            id="uc51x",
        ),
        pytest.param(
            "dragino",
            LHT65,
            "tts",
            2,
            "cbf60b0d0376010add7fff",
            {
                "temperature": "28.29",
                "humidity": "88.6",
                "external_temperature": "27.81",
                "battery_voltage": "3.062",
            },
            id="lht65-ttn-example",
        ),
    ],
)
async def test_entity_lifecycle(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    domain: str,
    model: type[Device],
    stack: str,
    port: int,
    payload: str,
    expected: dict[str, str],
) -> None:
    """Discover, decode, become unavailable, reconnect, and remove entities."""
    device = descriptor(model, stack)
    backend, unregister = await registered_backend("network", [device])
    entry = MockConfigEntry(domain=domain, data={}, unique_id=domain)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for key in expected:
        assert hass.states.get(f"sensor.catalog_device_{key}").state == "unknown"
    event = UplinkEvent(
        descriptor=device,
        received_at=dt_util.utcnow(),
        f_port=port,
        data=bytes.fromhex(payload),
    )
    backend._emit(event)
    await hass.async_block_till_done()
    for key, value in expected.items():
        assert hass.states.get(f"sensor.catalog_device_{key}").state == value
    assert not hass.states.async_entity_ids("switch")
    unregister()
    await hass.async_block_till_done()
    for key in expected:
        assert hass.states.get(f"sensor.catalog_device_{key}").state == "unavailable"
    reconnected, _ = await registered_backend("network", [device])
    await hass.async_block_till_done()
    for key, value in expected.items():
        assert hass.states.get(f"sensor.catalog_device_{key}").state == value
    reconnected._emit(RemovedEvent(descriptor=device, received_at=dt_util.utcnow()))
    await hass.async_block_till_done()
    for key in expected:
        assert hass.states.get(f"sensor.catalog_device_{key}") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("model", [S2102, TS201, UC51x, LHT65])
async def test_profile_name_does_not_grant_model_support(
    hass: HomeAssistant, registered_backend: RegisterBackend, model: type[Device]
) -> None:
    """A private profile must not select a model by its name or UUID."""
    device = replace(descriptor(model), model_id="", brand_id=None, name=model.__name__)
    await registered_backend("network", [device])
    for domain in ("sensecap", "milesight", "dragino"):
        entry = MockConfigEntry(domain=domain, data={})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert not entry.runtime_data.coordinators
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("model", "port", "payload", "attribute", "expected"),
    [
        (S2102, 2, "010310000000000000", "illuminance", 0),
        (TS201, 85, "93673401640002", "temperature", 30.8),
        (TS201, 85, "0367baff", "temperature", -7),
        (UC51x, 85, "030100", "valve_1_open", False),
        (LHT65, 2, "cbeaff9c026d01ff387fff", "external_temperature", -2),
    ],
)
def test_partial_invalid_and_old_uplinks(
    model: type[Device], port: int, payload: str, attribute: str, expected: float | bool
) -> None:
    """Decode source fixtures without losing readings to bad or unrelated data."""
    device = model(descriptor(model))
    event = UplinkEvent(
        descriptor=device.descriptor,
        received_at=dt_util.utcnow(),
        f_port=port,
        data=bytes.fromhex(payload),
    )
    device._receive_event(event)
    assert getattr(device, attribute) == expected
    device._receive_event(replace(event, data=b"\x01"))
    device._receive_event(replace(event, f_port=99, data=b"\x00" * 11))
    device._receive_event(
        replace(
            event,
            received_at=event.received_at - timedelta(seconds=1),
            data=b"\x00" * len(event.data),
        )
    )
    assert getattr(device, attribute) == expected


def test_sensor_fault_clears_temperature() -> None:
    """A TS201 fault makes the old measurement unknown."""
    device = TS201(descriptor(TS201))
    event = UplinkEvent(
        descriptor=device.descriptor,
        received_at=dt_util.utcnow(),
        f_port=85,
        data=bytes.fromhex("03673401"),
    )
    device._receive_event(event)
    device._receive_event(replace(event, data=bytes.fromhex("b36700")))
    assert device.temperature is None


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (TS201, "20cec79afa6402bdff"),
        (UC51x, "20ce3fa109641700000000"),
        (LHT65, "cbea0105026d4101aa7fff"),
    ],
)
def test_history_does_not_become_current_state(
    model: type[TS201 | UC51x | LHT65], payload: str
) -> None:
    """Device history replies do not report current telemetry."""
    assert model.decode(bytes.fromhex(payload)) == {}
