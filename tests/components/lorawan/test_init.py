"""Test provider availability, discovery and vendor routing."""

from collections.abc import Callable
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import (
    DeviceEvent,
    Downlink,
    EventType,
    StatusEvent,
    Unsubscribe,
)
from lorawan_connection.backend.chirpstack import (
    AuthenticationError,
    ConnectionUnavailable,
)
import pytest

from homeassistant.components.lorawan import (
    async_get_connections,
    async_register_connection,
)
from homeassistant.components.lorawan.connection import DATA_REGISTRY
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from .test_libraries import DESCRIPTOR, inventory

from tests.common import MockConfigEntry


async def test_provider_subscription(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Initial devices and later activity share the same vendor subscription."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    with patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as discovery:
        assert await hass.config_entries.async_setup(provider_entry.entry_id)
        connection = async_get_connections(hass)[provider_entry.entry_id]
        assert connection is not mock_connection
        assert {name for name in dir(connection) if not name.startswith("_")} == {
            "async_subscribe",
            "async_send_downlink",
            "on_disconnect",
        }
        await hass.async_block_till_done(wait_background_tasks=True)
        discovery.assert_awaited_once()
        assert discovery.call_args.kwargs["data"] == {}
        consumer, disconnected = Mock(), Mock()
        unsubscribe_disconnect = connection.on_disconnect(disconnected)
        unsubscribe = await connection.async_subscribe(
            brands=frozenset({("chirpstack", 744)}),
            callback=consumer,
        )
        consumer.assert_called_once()
        mock_connection._emit(
            inventory(replace(DESCRIPTOR, brand_id=1), EventType.UPDATED), DESCRIPTOR
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


async def test_disconnect(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Unavailable is visible before consumer reload and no stale replay."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    connection = async_get_connections(hass)[provider_entry.entry_id]
    disconnected = Mock()
    connection.on_disconnect(disconnected)
    await connection.async_subscribe(
        brands=frozenset({("chirpstack", 744)}), callback=Mock()
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_connection._failed(ConnectionUnavailable())
        disconnected.assert_called_once_with()
        reload.assert_called_once_with(provider_entry.entry_id)
    assert provider_entry.entry_id not in async_get_connections(hass)
    with pytest.raises(ConnectionUnavailable):
        await connection.async_subscribe(
            brands=frozenset({("chirpstack", 744)}), callback=Mock()
        )
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_downlink_uses_shared_connection(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """The consumer facade delegates commands to the provider transport."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    connection = async_get_connections(hass)[provider_entry.entry_id]
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
        await hass.async_block_till_done(wait_background_tasks=True)
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
            return_value={
                "one": [("chirpstack", 744), ("chirpstack", 676)],
                "two": [("chirpstack", 744)],
                "other": [("chirpstack", 123)],
            },
        ),
        patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as discovery,
    ):
        assert await hass.config_entries.async_setup(provider_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert [call.args[0] for call in discovery.await_args_list] == ["one", "two"]
        mock_connection._emit(inventory(DESCRIPTOR, EventType.UPDATED))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert discovery.await_count == 4
        await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_dismissed_discovery_returns(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Dismissal allows rediscovery; an in-progress flow remains deduplicated."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    mock_connection._emit(inventory(DESCRIPTOR, EventType.UPDATED))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(hass.config_entries.flow.async_progress()) == 1
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])
    mock_connection._emit(inventory(DESCRIPTOR, EventType.UPDATED))
    await hass.async_block_till_done(wait_background_tasks=True)
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["handler"] == "sensecap"
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_registration_failure(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Failed replay does not publish a half-registered connection."""
    mock_connection.async_subscribe.side_effect = ConnectionUnavailable("Replay failed")
    assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    assert provider_entry.state is ConfigEntryState.SETUP_RETRY
    assert not async_get_connections(hass)
    mock_connection.close.assert_awaited_once()


async def test_disconnect_during_registration(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Disconnect while awaiting replay prevents registration."""

    async def interrupted(**kwargs: object) -> Mock:
        mock_connection._failed(ConnectionUnavailable())
        return Mock()

    mock_connection.async_subscribe.side_effect = interrupted
    assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    assert not async_get_connections(hass)
    assert provider_entry.state is ConfigEntryState.SETUP_RETRY


async def test_duplicate_registration(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """A duplicate must not replace an existing active connection."""

    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    original = async_get_connections(hass)[provider_entry.entry_id]
    with pytest.raises(ValueError, match="already registered"):
        await async_register_connection(
            hass, provider_entry, connection=mock_connection
        )
    assert async_get_connections(hass)[provider_entry.entry_id] is original
    await hass.config_entries.async_unload(provider_entry.entry_id)


async def test_registration_failure_closes_transport(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Unexpected registration failures still release the server transport."""
    with patch(
        "homeassistant.components.lorawan.async_register_connection",
        side_effect=RuntimeError("registration failed"),
    ):
        assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    mock_connection.close.assert_awaited_once()
    assert provider_entry.entry_id not in async_get_connections(hass)


async def test_failed_reconnect_preserves_retained_status(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
) -> None:
    """Partial inventory from a failed registration cannot replace the saved state."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    report = StatusEvent(
        descriptor=DESCRIPTOR,
        received_at=dt_util.utcnow(),
        battery_level=42,
        battery_level_unavailable=False,
    )
    mock_connection._emit(report)
    original = hass.data[DATA_REGISTRY].inventories[provider_entry.entry_id]
    await hass.config_entries.async_unload(provider_entry.entry_id)

    async def failed_replay(
        *,
        brands: frozenset[tuple[str, int | str]] | None,
        callback: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        callback(inventory(replace(DESCRIPTOR, dev_eui="0201010101010102")))
        raise ConnectionUnavailable("incomplete replay")

    backend = Mock(
        on_disconnect=Mock(return_value=Mock()),
        async_subscribe=AsyncMock(side_effect=failed_replay),
    )
    with pytest.raises(ConnectionUnavailable, match="incomplete replay"):
        await async_register_connection(hass, provider_entry, connection=backend)
    assert hass.data[DATA_REGISTRY].inventories[provider_entry.entry_id] is original
    assert list(original.devices) == [DESCRIPTOR.dev_eui]
    assert original.devices[DESCRIPTOR.dev_eui].latest_status is report
    assert not async_get_connections(hass)


async def test_new_report_during_reconnect_wins_over_retained_status(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: Mock,
) -> None:
    """Do not replace fresh replay activity with the previous session's report."""
    mock_connection.devices = {DESCRIPTOR.dev_eui: DESCRIPTOR}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    old = StatusEvent(descriptor=DESCRIPTOR, received_at=dt_util.utcnow())
    mock_connection._emit(old)
    previous = hass.data[DATA_REGISTRY].inventories[provider_entry.entry_id]
    await hass.config_entries.async_unload(provider_entry.entry_id)
    fresh = replace(old, battery_level=25, battery_level_unavailable=False)

    async def replay(
        *,
        brands: frozenset[tuple[str, int | str]] | None,
        callback: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        callback(inventory(DESCRIPTOR))
        callback(fresh)
        return Mock()

    backend = Mock(
        on_disconnect=Mock(return_value=Mock()),
        async_subscribe=AsyncMock(side_effect=replay),
    )
    unsubscribe = await async_register_connection(
        hass, provider_entry, connection=backend
    )
    current = hass.data[DATA_REGISTRY].inventories[provider_entry.entry_id]
    assert not previous.devices
    assert current.devices[DESCRIPTOR.dev_eui].latest_status is fresh
    unsubscribe()
