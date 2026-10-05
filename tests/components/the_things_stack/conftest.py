"""The Things Stack transport boundary fixtures."""

from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

from lorawan_connection.backend.tts import TTSConnection
import pytest

from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry
from tests.components.lorawan.conftest import registered_backend  # noqa: F401


@pytest.fixture
def provider_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add a configured TTS server."""
    entry = MockConfigEntry(
        domain="the_things_stack",
        entry_id="tts-server",
        title="The Things Stack",
        unique_id="http://server:1884|http://identity:1884|app",
        data={
            "endpoint": "http://server:1884",
            "identity_server": "http://identity:1884",
            "api_key": "secret",
            "application_ids": ["app"],
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_connection() -> Generator[TTSConnection]:
    """Keep actual subscriptions and disconnects, while replacing transport I/O."""
    with patch("grpc.aio.insecure_channel", return_value=Mock(close=AsyncMock())):
        connection = TTSConnection(
            "http://server:1884",
            "secret",
            application_ids=["app"],
            network_id="tts-server",
        )

    async def connect() -> None:
        connection.available = True
        connection._closed = False

    connection.async_connect = AsyncMock(side_effect=connect)
    connection.close = AsyncMock(wraps=connection.close)
    with (
        patch(
            "homeassistant.components.the_things_stack.TTSConnection",
            return_value=connection,
        ),
        patch(
            "homeassistant.components.the_things_stack.config_flow.TTSConnection",
            return_value=connection,
        ),
    ):
        yield connection
