"""The Things Stack setup and reauthentication."""

from unittest.mock import patch

from lorawan_connection import ConnectionUnavailable
from lorawan_connection.backend.tts import AuthenticationError, TTSConnection
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry

INPUT = {
    "endpoint": "http://server:1884",
    "identity_server": "http://identity:1884",
    "api_key": "secret",
    "application_ids": ["app"],
}


async def test_setup(hass: HomeAssistant, mock_connection: TTSConnection) -> None:
    """Validate both connections and store a provider without vendor selection."""
    result = await hass.config_entries.flow.async_init(
        "the_things_stack", context={"source": SOURCE_USER}, data=INPUT
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == INPUT
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["result"].runtime_data.connection is mock_connection
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_form(hass: HomeAssistant) -> None:
    """Display server and application fields."""
    result = await hass.config_entries.flow.async_init(
        "the_things_stack", context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError(), "invalid_auth"),
        (ConnectionUnavailable(), "cannot_connect"),
    ],
)
async def test_errors(
    hass: HomeAssistant, mock_connection: TTSConnection, error: Exception, expected: str
) -> None:
    """Reject invalid credentials and unavailable servers without saving entries."""
    mock_connection.async_connect.side_effect = error
    result = await hass.config_entries.flow.async_init(
        "the_things_stack", context={"source": SOURCE_USER}, data=INPUT
    )
    assert result["errors"] == {"base": expected}
    mock_connection.close.assert_awaited_once()


async def test_invalid_endpoint(
    hass: HomeAssistant, mock_connection: TTSConnection
) -> None:
    """Report malformed server URLs."""
    with patch(
        "homeassistant.components.the_things_stack.config_flow.TTSConnection",
        side_effect=ValueError,
    ):
        result = await hass.config_entries.flow.async_init(
            "the_things_stack", context={"source": SOURCE_USER}, data=INPUT
        )
    assert result["errors"] == {"base": "invalid_endpoint"}


async def test_empty_apps(hass: HomeAssistant, mock_connection: TTSConnection) -> None:
    """Explicit application scope is required for restricted keys."""
    result = await hass.config_entries.flow.async_init(
        "the_things_stack",
        context={"source": SOURCE_USER},
        data={**INPUT, "application_ids": []},
    )
    assert result["errors"] == {"application_ids": "select_application"}
    mock_connection.async_connect.assert_not_called()


async def test_duplicate(
    hass: HomeAssistant, mock_connection: TTSConnection, provider_entry: MockConfigEntry
) -> None:
    """The same endpoint and application scope has one provider."""
    result = await hass.config_entries.flow.async_init(
        "the_things_stack", context={"source": SOURCE_USER}, data=INPUT
    )
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, mock_connection: TTSConnection, provider_entry: MockConfigEntry
) -> None:
    """Replace the key while preserving the config entry ID and application scope."""
    result = await provider_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "replacement"}
    )
    assert result["reason"] == "reauth_successful"
    assert provider_entry.data["api_key"] == "replacement"
    assert provider_entry.entry_id == "tts-server"
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.config_entries.async_unload(provider_entry.entry_id)
