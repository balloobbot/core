"""Test provider availability, discovery and vendor routing."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import Downlink, DownlinkError, EventType
from lorawan_connection.chirpstack import AuthenticationError, ConnectionUnavailable
import pytest

from homeassistant.components import lorawan
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry


async def test_provider_subscription(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Initial devices and later activity share the same vendor subscription."""
    mock_connection.async_subscribe.side_effect = lambda callback, disconnected: (
        callback(inventory())
    )
    with patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as discovery:
        assert await hass.config_entries.async_setup(provider_entry.entry_id)
        assert lorawan.get_connection(hass, provider_entry.entry_id) is mock_connection
        await hass.async_block_till_done()
        discovery.assert_awaited_once()
        consumer, disconnected = Mock(), Mock()
        stop = await lorawan.async_subscribe(
            hass, provider_entry.entry_id, frozenset({744}), consumer, disconnected
        )
        consumer.assert_called_once()
        callback = mock_connection.async_subscribe.call_args.args[0]
        callback(inventory(replace(DESCRIPTOR, vendor_id=1), EventType.UPDATED))
        assert consumer.call_args.args[0].type == EventType.REMOVED
        stop()
        stop()
        await hass.config_entries.async_unload(provider_entry.entry_id)
        disconnected.assert_not_called()
        mock_connection.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (ConnectionUnavailable(), ConfigEntryState.SETUP_RETRY),
        (AuthenticationError(), ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_setup_failure(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    """Connection failures retry; invalid credentials start reauthentication."""
    mock_connection.async_subscribe.side_effect = error
    assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    assert provider_entry.state == state


async def test_unavailable_subscription(
    hass: HomeAssistant, provider_entry: MockConfigEntry
) -> None:
    """Unloaded and missing providers cannot accept subscriptions."""
    with pytest.raises(ConnectionUnavailable):
        lorawan.get_connection(hass, provider_entry.entry_id)
    with pytest.raises(ConnectionUnavailable):
        lorawan.get_connection(hass, "missing")
    with pytest.raises(ConnectionUnavailable):
        await lorawan.async_subscribe(
            hass, provider_entry.entry_id, frozenset({744}), Mock(), Mock()
        )


async def test_disconnect(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Unavailable is visible before consumer reload and no stale replay."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    disconnected = Mock()
    await lorawan.async_subscribe(
        hass, provider_entry.entry_id, frozenset({744}), Mock(), disconnected
    )
    mock_connection.available = False
    with pytest.raises(ConnectionUnavailable):
        lorawan.get_connection(hass, provider_entry.entry_id)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_connection.async_subscribe.call_args.args[1](ConnectionUnavailable())
        disconnected.assert_called_once()
        reload.assert_called_once_with(provider_entry.entry_id)
    with pytest.raises(ConnectionUnavailable):
        await lorawan.async_subscribe(
            hass, provider_entry.entry_id, frozenset({744}), Mock(), Mock()
        )
    await hass.config_entries.async_unload(provider_entry.entry_id)


@pytest.mark.parametrize("condition", ["missing", "offline", "unknown"])
async def test_downlink_requires_available_inventory(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
    condition: str,
) -> None:
    """Never send a command through a missing provider or to an unknown device."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    mock_connection.async_send_downlink = AsyncMock()
    provider_id = provider_entry.entry_id
    if condition == "missing":
        provider_id = "missing"
    elif condition == "offline":
        mock_connection.available = False
    with pytest.raises(DownlinkError):
        await lorawan.async_send_downlink(
            hass, provider_id, Downlink(DESCRIPTOR.dev_eui, 2, b"command")
        )
    mock_connection.async_send_downlink.assert_not_awaited()
    await hass.config_entries.async_unload(provider_entry.entry_id)
