"""Configuration and authentication flows."""

from unittest.mock import Mock

import grpc

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry


async def test_global_key(hass: HomeAssistant, mock_connection: Mock) -> None:
    """A global key discovers the sole tenant and offers applications."""
    result = await hass.config_entries.flow.async_init(
        "lorawan",
        context={"source": SOURCE_USER},
        data={"endpoint": "http://server:8080", "api_key": "secret"},
    )
    assert result["step_id"] == "applications"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"application_ids": ["application"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["tenant_id"] == "tenant"
    assert result["data"]["network_id"]
    await hass.async_block_till_done()


async def test_scoped_key(hass: HomeAssistant, mock_connection: Mock) -> None:
    """Scoped keys need a tenant UUID but do not require write access."""
    mock_connection.tenants.side_effect = grpc.aio.AioRpcError(
        grpc.StatusCode.UNAUTHENTICATED, (), (), ""
    )
    result = await hass.config_entries.flow.async_init(
        "lorawan",
        context={"source": SOURCE_USER},
        data={"endpoint": "http://server:8080", "api_key": "readonly"},
    )
    assert result["step_id"] == "tenant"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"tenant_id": "tenant"}
    )
    assert result["step_id"] == "applications"


async def test_invalid_auth(hass: HomeAssistant, mock_connection: Mock) -> None:
    """A tenant read disambiguates a scoped key from a rejected credential."""
    mock_connection.tenants.side_effect = grpc.aio.AioRpcError(
        grpc.StatusCode.UNAUTHENTICATED, (), (), "bad key"
    )
    result = await hass.config_entries.flow.async_init(
        "lorawan",
        context={"source": SOURCE_USER},
        data={"endpoint": "http://server:8080", "api_key": "bad"},
    )
    assert result["step_id"] == "tenant"
    mock_connection.applications.side_effect = grpc.aio.AioRpcError(
        grpc.StatusCode.UNAUTHENTICATED, (), (), ""
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"tenant_id": "tenant"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth(
    hass: HomeAssistant, provider_entry: MockConfigEntry, mock_connection: Mock
) -> None:
    """Replace the key while preserving network and device identity."""
    result = await provider_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "replacement"}
    )
    assert result["reason"] == "reauth_successful"
    assert provider_entry.data["api_key"] == "replacement"
    assert provider_entry.data["network_id"] == "network"
    await hass.async_block_till_done()
