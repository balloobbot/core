"""Provider registration and isolated mixed-backend lifecycle."""

from dataclasses import replace
from unittest.mock import patch

from lorawan_connection import ConnectionUnavailable
from lorawan_connection.backend.tts import AuthenticationError, TTSConnection
import pytest

from homeassistant.components.lorawan import async_get_connections
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant

from tests.common import MockConfigEntry
from tests.components.lorawan.conftest import RegisterBackend
from tests.components.lorawan.test_libraries import DESCRIPTOR


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (AuthenticationError(), ConfigEntryState.SETUP_ERROR),
        (ConnectionUnavailable(), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: TTSConnection,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    """Authentication starts reauth; network failures retry."""
    mock_connection.async_connect.side_effect = error
    assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    assert provider_entry.state is state
    mock_connection.close.assert_awaited_once()


async def test_mixed_connections(
    hass: HomeAssistant,
    provider_entry: MockConfigEntry,
    mock_connection: TTSConnection,
    registered_backend: RegisterBackend,
) -> None:
    """One vendor handles both stacks and keeps ChirpStack live during TTS recovery."""
    await registered_backend("network", [DESCRIPTOR])
    tts = replace(
        DESCRIPTOR,
        network_id=provider_entry.entry_id,
        application_id="app",
        name="Bedroom",
        stack="tts",
        brand_id="sensecap",
        model_id="sensecaps2101-temp-humid",
    )
    mock_connection.devices = {tts.dev_eui: tts}
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    vendor = MockConfigEntry(domain="sensecap")
    vendor.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vendor.entry_id)
    await hass.async_block_till_done()
    manager = vendor.runtime_data
    coordinator = manager.coordinators[(provider_entry.entry_id, tts.dev_eui)]
    assert len(manager.coordinators) == 2
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_connection._failed(ConnectionUnavailable())
        await hass.async_block_till_done()
    reload.assert_called_once_with(provider_entry.entry_id)
    assert hass.states.get("sensor.bedroom_temperature").state == "unavailable"
    assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
    assert vendor.state is ConfigEntryState.LOADED
    assert provider_entry.entry_id not in async_get_connections(hass)
    assert await hass.config_entries.async_reload(provider_entry.entry_id)
    await hass.async_block_till_done()
    assert manager.coordinators[(provider_entry.entry_id, tts.dev_eui)] is coordinator
    assert hass.states.get("sensor.bedroom_temperature").state == "unknown"
    await hass.config_entries.async_remove(provider_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bedroom_temperature") is None
    assert hass.states.get("sensor.greenhouse_temperature") is not None
    assert len(manager.coordinators) == 1
    await hass.config_entries.async_unload(vendor.entry_id)


async def test_shutdown(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: TTSConnection
) -> None:
    """Shutdown closes streams without scheduling recovery."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    hass.set_state(CoreState.stopping)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert provider_entry.entry_id not in async_get_connections(hass)
    mock_connection.close.assert_awaited_once()
    reload.assert_not_called()


async def test_registration_failure_closes_transport(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: TTSConnection
) -> None:
    """Unexpected registration failures still release the server transport."""
    with patch(
        "homeassistant.components.lorawan.async_register_connection",
        side_effect=RuntimeError("registration failed"),
    ):
        assert not await hass.config_entries.async_setup(provider_entry.entry_id)
    mock_connection.close.assert_awaited_once()
    assert provider_entry.entry_id not in async_get_connections(hass)


async def test_disconnect_during_shutdown(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: TTSConnection
) -> None:
    """A dropped connection during shutdown does not start recovery."""
    assert await hass.config_entries.async_setup(provider_entry.entry_id)
    hass.set_state(CoreState.stopping)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_connection._failed(ConnectionUnavailable())
    reload.assert_not_called()
    await hass.config_entries.async_unload(provider_entry.entry_id)
