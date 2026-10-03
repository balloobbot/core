"""LoRaWAN provider fixtures."""

from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection.chirpstack import ChirpStackConnection
import pytest

from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry


@pytest.fixture
def provider_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Configured external network."""
    entry = MockConfigEntry(
        domain="lorawan",
        title="LoRaWAN",
        unique_id="server:tenant",
        data={
            "endpoint": "http://server:8080",
            "api_key": "secret",
            "tenant_id": "tenant",
            "application_ids": ["application"],
            "network_id": "network",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_connection() -> Generator[Mock]:
    """Mock only the transport boundary, not provider or vendor wiring."""
    connection = ChirpStackConnection(
        "http://server:8080",
        "secret",
        tenant_id="tenant",
        application_ids=["application"],
        network_id="network",
        channel=Mock(close=AsyncMock()),
    )
    connection.async_connect = AsyncMock(
        side_effect=lambda: setattr(connection, "available", True)
    )
    connection.async_send_downlink = AsyncMock(return_value="queue-id")
    connection.async_subscribe = AsyncMock(wraps=connection.async_subscribe)
    connection.close = AsyncMock(wraps=connection.close)
    connection.tenants = AsyncMock(return_value={"tenant": "Home"})
    connection.applications = AsyncMock(return_value={"application": "Sensors"})
    with (
        patch(
            "homeassistant.components.lorawan.ChirpStackConnection",
            return_value=connection,
        ),
        patch(
            "homeassistant.components.lorawan.config_flow.ChirpStackConnection",
            return_value=connection,
        ),
    ):
        yield connection
