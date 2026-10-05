"""LoRaWAN provider fixtures."""

from collections.abc import Awaitable, Callable, Generator
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection import DeviceDescriptor, Unsubscribe
from lorawan_connection.chirpstack import ChirpStackConnection
import pytest

from homeassistant.components.lorawan import async_register_connection
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from tests.common import MockConfigEntry


@pytest.fixture
def provider_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Configured external network."""
    entry = MockConfigEntry(
        domain="chirpstack",
        entry_id="network",
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
    connection.inventory = AsyncMock(return_value=())
    connection.tenants = AsyncMock(return_value={"tenant": "Home"})
    connection.applications = AsyncMock(return_value={"application": "Sensors"})
    with (
        patch(
            "homeassistant.components.chirpstack.ChirpStackConnection",
            return_value=connection,
        ),
        patch(
            "homeassistant.components.chirpstack.config_flow.ChirpStackConnection",
            return_value=connection,
        ),
    ):
        yield connection


type RegisterBackend = Callable[
    [str, list[DeviceDescriptor]], Awaitable[tuple[ChirpStackConnection, Unsubscribe]]
]


@pytest.fixture
def registered_backend(hass: HomeAssistant) -> Generator[RegisterBackend]:
    """Create real subscription backends with network I/O stubbed out."""
    cleanups: list[Unsubscribe] = []

    async def register(
        entry_id: str, descriptors: list[DeviceDescriptor]
    ) -> tuple[ChirpStackConnection, Unsubscribe]:
        assert await async_setup_component(hass, "lorawan", {})
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            entry = MockConfigEntry(domain="chirpstack", entry_id=entry_id)
            entry.add_to_hass(hass)
        backend = ChirpStackConnection(
            "http://server:8080",
            "secret",
            tenant_id="tenant",
            application_ids=["application"],
            network_id=entry_id,
            channel=Mock(close=AsyncMock()),
        )
        backend.available = True
        backend.devices = {device.dev_eui: device for device in descriptors}
        unsubscribe = await async_register_connection(hass, entry, connection=backend)
        cleanups.append(unsubscribe)
        return backend, unsubscribe

    yield register
    for unsubscribe in cleanups:
        unsubscribe()
