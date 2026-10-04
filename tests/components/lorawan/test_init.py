"""Test provider availability, discovery and vendor routing."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import Downlink, EventType
from lorawan_connection.chirpstack import AuthenticationError, ConnectionUnavailable
import pytest

from homeassistant.components import lorawan
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant

from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry


async def test_provider_subscription(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Initial devices and later activity share the same vendor subscription."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    with patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as discovery:
        assert await hass.config_entries.async_setup(provider_entry.entry_id)
        connection = lorawan.get_connection(hass, provider_entry.entry_id)
        assert connection is not mock_connection
        assert {name for name in dir(connection) if not name.startswith("_")} == {
            "async_subscribe",
            "async_send_downlink",
            "on_disconnect",
        }
        await hass.async_block_till_done()
        discovery.assert_awaited_once()
        consumer, disconnected = Mock(), Mock()
        unsubscribe_disconnect = connection.on_disconnect(disconnected)
        unsubscribe = await connection.async_subscribe(
            vendor_ids=frozenset({744}),
            callback=consumer,
        )
        consumer.assert_called_once()
        mock_connection._emit(
            inventory(replace(DESCRIPTOR, vendor_id=1), EventType.UPDATED), DESCRIPTOR
        )
        assert consumer.call_args.args[0].type == EventType.REMOVED
        unsubscribe()
        unsubscribe()
        unsubscribe_disconnect()
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
    mock_connection.async_connect.side_effect = error
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


async def test_disconnect(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Unavailable is visible before consumer reload and no stale replay."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    connection = lorawan.get_connection(hass, provider_entry.entry_id)
    disconnected = Mock()
    connection.on_disconnect(disconnected)
    await connection.async_subscribe(vendor_ids=frozenset({744}), callback=Mock())
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_connection._failed(ConnectionUnavailable())
        disconnected.assert_called_once_with()
        reload.assert_called_once_with(provider_entry.entry_id)
    with pytest.raises(ConnectionUnavailable):
        lorawan.get_connection(hass, provider_entry.entry_id)
    with pytest.raises(ConnectionUnavailable):
        await connection.async_subscribe(vendor_ids=frozenset({744}), callback=Mock())
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_downlink_uses_shared_connection(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """The consumer facade delegates commands to the provider transport."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    connection = lorawan.get_connection(hass, provider_entry.entry_id)
    downlink = Downlink(DESCRIPTOR.dev_eui, 2, b"command")
    assert await connection.async_send_downlink(downlink) == "queue-id"
    mock_connection.async_send_downlink.assert_awaited_once_with(downlink)
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_home_assistant_shutdown(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
) -> None:
    """Closing HA also closes the provider without scheduling a reconnect."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    hass.set_state(CoreState.stopping)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    mock_connection.close.assert_awaited_once()
    assert not mock_connection.available
    reload.assert_not_called()


async def test_manifest_vendor_discovery(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
) -> None:
    """Multiple integration registrations can match a vendor without hardcoding."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    with (
        patch(
            "homeassistant.components.lorawan.async_get_lorawan",
            return_value={"one": [744, 676], "two": [744], "other": [123]},
        ),
        patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as discovery,
    ):
        assert await hass.config_entries.async_setup(provider_entry.entry_id)
        await hass.async_block_till_done()
        assert [call.args[0] for call in discovery.await_args_list] == ["one", "two"]
        mock_connection._emit(inventory(DESCRIPTOR, EventType.UPDATED))
        await hass.async_block_till_done()
        assert discovery.await_count == 2
        await hass.config_entries.async_unload(provider_entry.entry_id)
