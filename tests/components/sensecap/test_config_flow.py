"""Select a LoRaWAN network for a SenseCAP collection."""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry


@pytest.fixture(autouse=True)
def mock_provider_setup() -> Generator[None]:
    """Keep config flow tests independent of the network server."""
    with patch("homeassistant.components.lorawan.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def provider(hass: HomeAssistant) -> MockConfigEntry:
    """Register a LoRaWAN network without connecting to a server."""
    entry = MockConfigEntry(domain="lorawan", title="Home", data={"network_id": "home"})
    entry.add_to_hass(hass)
    return entry


async def test_no_provider(hass: HomeAssistant) -> None:
    """A different integration does not count as a LoRaWAN provider."""
    MockConfigEntry(domain="demo").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_provider"


async def test_single_provider(hass: HomeAssistant, provider: MockConfigEntry) -> None:
    """Skip selection and save the sole provider after confirmation."""
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    with patch(
        "homeassistant.components.sensecap.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "connection_entry_id": provider.entry_id,
        "network_id": "home",
    }
    assert result["result"].unique_id == "home"


@pytest.mark.usefixtures("provider")
async def test_multiple_providers(hass: HomeAssistant) -> None:
    """Use the chosen provider when more than one network is configured."""
    second = MockConfigEntry(
        domain="lorawan", title="Garden", data={"network_id": "garden"}
    )
    second.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["data_schema"].schema["connection_entry_id"].config == {
        "integration": "lorawan"
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"connection_entry_id": second.entry_id}
    )
    assert result["step_id"] == "confirm"
    with patch(
        "homeassistant.components.sensecap.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "connection_entry_id": second.entry_id,
        "network_id": "garden",
    }
    assert result["result"].unique_id == "garden"


@pytest.mark.usefixtures("provider")
@pytest.mark.parametrize("provider_id", ["missing", "other_integration"])
async def test_invalid_provider(hass: HomeAssistant, provider_id: str) -> None:
    """Reject removed entries and entries from other integrations."""
    MockConfigEntry(domain="demo", entry_id="other_integration").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "sensecap",
        context={"source": SOURCE_USER},
        data={"connection_entry_id": provider_id},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_provider"}


async def test_already_configured(
    hass: HomeAssistant, provider: MockConfigEntry
) -> None:
    """Manual setup cannot duplicate a discovered collection."""
    MockConfigEntry(
        domain="sensecap",
        unique_id="home",
        data={"connection_entry_id": provider.entry_id, "network_id": "home"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
