"""Inventory API access control and credential isolation."""

from unittest.mock import Mock

from homeassistant.core import HomeAssistant

from .test_libraries import DESCRIPTOR

from tests.common import MockConfigEntry
from tests.typing import WebSocketGenerator


async def test_inventory(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Return descriptors without exposing the API key."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "lorawan/devices", "entry_id": provider_entry.entry_id}
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"]["devices"][0]["vendor_id"] == 744
    assert "secret" not in str(result)
    await client.send_json({"id": 2, "type": "lorawan/devices", "entry_id": "missing"})
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
        {"id": 1, "type": "lorawan/devices", "entry_id": provider_entry.entry_id}
    )
    assert not (await client.receive_json())["success"]
    await hass.config_entries.async_unload(provider_entry.entry_id)
