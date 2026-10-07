"""Inventory API access control and credential isolation."""

from asyncio import Event
from dataclasses import replace
from unittest.mock import Mock, patch

from lorawan_connection import (
    ConnectionUnavailable,
    DeviceDescriptor,
    RemovedEvent,
    StatusEvent,
    UpdatedEvent,
)
import pytest

from homeassistant.components.lorawan.connection import DATA_REGISTRY
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import RegisterBackend
from .test_libraries import DESCRIPTOR

from tests.common import MockConfigEntry
from tests.typing import WebSocketGenerator


async def test_inventory_during_unload(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """An import awaiting completion keeps the request's original device snapshot."""
    second = replace(DESCRIPTOR, dev_eui="0201010101010102")
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR, second.dev_eui: second}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    client = await hass_ws_client(hass)
    entered, release = Event(), Event()

    async def support(
        hass: HomeAssistant,
        device: DeviceDescriptor,
        integrations: dict[str, list[tuple[str, int | str]]],
    ) -> None:
        entered.set()
        await release.wait()

    with patch(
        "homeassistant.components.lorawan.websocket_api.async_unsupported_reason",
        side_effect=support,
    ):
        await client.send_json(
            {
                "id": 1,
                "type": "lorawan/devices/list",
                "entry_id": provider_entry.entry_id,
            }
        )
        await entered.wait()
        assert await hass.config_entries.async_unload(provider_entry.entry_id)
        release.set()
        result = await client.receive_json()
    assert result["success"]
    assert [device["dev_eui"] for device in result["result"]["devices"]] == [
        DESCRIPTOR.dev_eui,
        second.dev_eui,
    ]


@pytest.mark.parametrize(
    ("descriptor", "reason"),
    [
        pytest.param(DESCRIPTOR, None, id="supported-sensecap"),
        pytest.param(
            replace(
                DESCRIPTOR,
                stack="tts",
                brand_id="sensecap",
                model_id="sensecaps2101-temp-humid",
            ),
            None,
            id="supported-tts-sensecap",
        ),
        pytest.param(
            replace(DESCRIPTOR, stack="tts", brand_id=744),
            "no_vendor_integration",
            id="stack-namespace-is-required",
        ),
        pytest.param(
            replace(DESCRIPTOR, stack="tts", brand_id="sensecap"),
            "model_not_supported",
            id="chirpstack-model-id-does-not-match-tts",
        ),
        pytest.param(
            replace(
                DESCRIPTOR,
                brand_id=676,
                model_id="cb0a7bef-eaa0-4c61-a0b6-ce33e6ecbc4f",
            ),
            None,
            id="supported-dragino",
        ),
        pytest.param(
            replace(DESCRIPTOR, brand_id=None, model_id=""),
            "no_catalog_identity",
            id="custom-profile",
        ),
        pytest.param(
            replace(DESCRIPTOR, model_id=""),
            "no_catalog_identity",
            id="missing-model-identity",
        ),
        pytest.param(
            replace(DESCRIPTOR, brand_id=None),
            "no_catalog_identity",
            id="missing-vendor-identity",
        ),
        pytest.param(
            replace(DESCRIPTOR, brand_id=999, model_id=""),
            "no_catalog_identity",
            id="missing-identity-before-vendor-match",
        ),
        pytest.param(
            replace(DESCRIPTOR, brand_id=999),
            "no_vendor_integration",
            id="unregistered-vendor",
        ),
        pytest.param(
            replace(DESCRIPTOR, model_id="another-model"),
            "model_not_supported",
            id="unsupported-sensecap-model",
        ),
        pytest.param(
            replace(DESCRIPTOR, brand_id=676, model_id="another-model"),
            "model_not_supported",
            id="unsupported-dragino-model",
        ),
    ],
)
async def test_inventory(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    hass_ws_client: WebSocketGenerator,
    descriptor: DeviceDescriptor,
    reason: str | None,
) -> None:
    """Return descriptors without exposing the API key."""
    mock_connection.devices = {descriptor.dev_eui: descriptor}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "lorawan/devices/list", "entry_id": provider_entry.entry_id}
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"]["devices"][0]["brand_id"] == descriptor.brand_id
    assert result["result"]["devices"][0]["unsupported_reason"] == reason
    assert result["result"]["available"] is True
    assert "secret" not in str(result)
    await client.send_json(
        {"id": 2, "type": "lorawan/devices/list", "entry_id": "missing"}
    )
    assert not (await client.receive_json())["success"]
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_admin_required(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """A regular user cannot enumerate the network inventory."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json(
        {"id": 1, "type": "lorawan/devices/list", "entry_id": provider_entry.entry_id}
    )
    assert not (await client.receive_json())["success"]
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_support_changes_with_profile(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Assigning a catalog profile clears the reason before vendor setup."""
    descriptor = replace(DESCRIPTOR, model_id="", brand_id=None)
    mock_connection.devices = {descriptor.dev_eui: descriptor}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    client = await hass_ws_client(hass)
    request = {"type": "lorawan/devices/list", "entry_id": provider_entry.entry_id}
    await client.send_json({"id": 1, **request})
    result = await client.receive_json()
    assert result["result"]["devices"][0]["unsupported_reason"] == "no_catalog_identity"
    mock_connection.devices[DESCRIPTOR.dev_eui] = DESCRIPTOR
    mock_connection._emit(
        UpdatedEvent(received_at=dt_util.utcnow(), descriptor=DESCRIPTOR)
    )
    await client.send_json({"id": 2, **request})
    result = await client.receive_json()
    assert result["result"]["devices"][0]["unsupported_reason"] is None
    assert not hass.config_entries.async_entries("sensecap")
    await hass.config_entries.async_unload(provider_entry.entry_id)


@pytest.mark.parametrize(
    ("level", "unavailable", "external", "expected"),
    [
        (64.0, False, False, 64.0),
        (0.0, False, False, 0.0),
        (64.0, True, False, None),
        (64.0, False, True, None),
    ],
)
async def test_status_collected_before_first_request(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    hass_ws_client: WebSocketGenerator,
    level: float,
    unavailable: bool,
    external: bool,
    expected: float | None,
) -> None:
    """Unknown devices retain MAC status without a configured vendor or viewer."""
    descriptor = replace(DESCRIPTOR, brand_id=None, model_id="")
    backend, _ = await registered_backend("network", [descriptor])
    received_at = dt_util.utcnow()
    backend._emit(
        StatusEvent(
            descriptor=descriptor,
            received_at=received_at,
            battery_level=level,
            battery_level_unavailable=unavailable,
            external_power_source=external,
            margin=-4,
        )
    )
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "lorawan/devices/list", "entry_id": "network"}
    )
    result = (await client.receive_json())["result"]
    assert result["available"] is True
    device = result["devices"][0]
    assert device["unsupported_reason"] == "no_catalog_identity"
    assert device["status"] == {
        "received_at": received_at.isoformat(),
        "battery_level": expected,
        "external_power_source": external,
        "downlink_margin": -4,
    }
    assert not hass.config_entries.async_entries("sensecap")


async def test_status_survives_disconnect_and_reconciles_reconnect(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Retain timestamps offline, isolate networks, and drop absent devices on replay."""
    missing = replace(DESCRIPTOR, dev_eui="0201010101010102")
    backend, _ = await registered_backend("network", [DESCRIPTOR, missing])
    other = replace(DESCRIPTOR, network_id="other")
    await registered_backend("other", [other])
    received_at = dt_util.utcnow()
    backend._emit(
        StatusEvent(
            descriptor=DESCRIPTOR,
            received_at=received_at,
            battery_level=50,
            battery_level_unavailable=False,
        )
    )
    backend._failed(ConnectionUnavailable("offline"))
    client = await hass_ws_client(hass)
    request = {"type": "lorawan/devices/list", "entry_id": "network"}
    await client.send_json({"id": 1, **request})
    offline = (await client.receive_json())["result"]
    assert offline["available"] is False
    assert len(offline["devices"]) == 2
    assert offline["devices"][0]["status"]["battery_level"] == 50
    await client.send_json(
        {"id": 2, "type": "lorawan/devices/list", "entry_id": "other"}
    )
    isolated = (await client.receive_json())["result"]
    assert isolated["available"] is True
    assert isolated["devices"][0]["status"] is None

    recovered, _ = await registered_backend("network", [DESCRIPTOR])
    await client.send_json({"id": 3, **request})
    online = (await client.receive_json())["result"]
    assert online["available"] is True
    assert len(online["devices"]) == 1
    assert online["devices"][0]["status"] == offline["devices"][0]["status"]
    recovered._emit(RemovedEvent(descriptor=DESCRIPTOR, received_at=dt_util.utcnow()))
    await client.send_json({"id": 4, **request})
    assert (await client.receive_json())["result"]["devices"] == []


async def test_deleted_entry_discards_retained_inventory(
    hass: HomeAssistant,
    registered_backend: RegisterBackend,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Deleting a disconnected server releases its in-memory state."""
    backend, _ = await registered_backend("network", [DESCRIPTOR])
    collection = hass.data[DATA_REGISTRY].inventories["network"]
    backend._failed(ConnectionUnavailable("offline"))
    assert await hass.config_entries.async_remove("network")
    await hass.async_block_till_done()
    assert not collection.devices
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "lorawan/devices/list", "entry_id": "network"}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"
