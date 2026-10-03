"""LoRaWAN provider fixtures."""

from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

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
    connection = Mock(
        network_id="network",
        async_send_downlink=AsyncMock(return_value="queue-id"),
        available=True,
        async_subscribe=AsyncMock(),
        close=AsyncMock(),
        tenants=AsyncMock(return_value={"tenant": "Home"}),
        applications=AsyncMock(return_value={"application": "Sensors"}),
    )
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
