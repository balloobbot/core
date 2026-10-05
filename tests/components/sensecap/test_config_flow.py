"""Configure one SenseCAP entry for all server connections."""

from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry


@pytest.mark.parametrize("source", [SOURCE_USER, SOURCE_INTEGRATION_DISCOVERY])
async def test_single_entry(hass: HomeAssistant, source: str) -> None:
    """Setup has no server selector and discovers the vendor only once."""
    result = await hass.config_entries.flow.async_init(
        "sensecap",
        context={"source": source},
        data={} if source == SOURCE_INTEGRATION_DISCOVERY else None,
    )
    assert result["step_id"] == "confirm"
    with patch(
        "homeassistant.components.sensecap.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert result["result"].unique_id == "sensecap"
    result = await hass.config_entries.flow.async_init(
        "sensecap",
        context={"source": source},
        data={} if source == SOURCE_INTEGRATION_DISCOVERY else None,
    )
    assert result["reason"] == "already_configured"


async def test_multiple_connections(hass: HomeAssistant) -> None:
    """Several server entries still produce one confirmation, with no chooser."""
    MockConfigEntry(domain="chirpstack").add_to_hass(hass)
    MockConfigEntry(domain="chirpstack").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "sensecap", context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "confirm"
