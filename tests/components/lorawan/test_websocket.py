"""Inventory API access control and credential isolation."""

from asyncio import Event
from dataclasses import replace
from unittest.mock import Mock, patch

from lorawan_connection import DeviceDescriptor, DeviceEventData, EventType
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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
        DeviceEventData(
            type=EventType.UPDATED, received_at=dt_util.utcnow(), descriptor=DESCRIPTOR
        )
    )
    await client.send_json({"id": 2, **request})
    result = await client.receive_json()
    assert result["result"]["devices"][0]["unsupported_reason"] is None
    assert not hass.config_entries.async_entries("sensecap")
    await hass.config_entries.async_unload(provider_entry.entry_id)
