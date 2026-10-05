"""Admin-only inventory for a future LoRaWAN UI."""

from dataclasses import asdict
from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .connection import DATA_REGISTRY
from .discovery import async_unsupported_reason


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register inventory access without exposing endpoint credentials."""
    websocket_api.async_register_command(hass, websocket_devices)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        probatio.Required("type"): "lorawan/devices/list",
        probatio.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return device descriptors, support reasons, and provider availability."""
    registry = hass.data[DATA_REGISTRY]
    registered = registry.connections.get(msg["entry_id"])
    if registered is None:
        connection.send_error(
            msg["id"], "not_found", "LoRaWAN connection is not registered"
        )
        return
    connection.send_result(
        msg["id"],
        {
            "available": True,
            "devices": [
                {
                    **asdict(device),
                    "unsupported_reason": await async_unsupported_reason(
                        hass, device, registry.integrations
                    ),
                }
                for device in tuple(registered.devices.values())
            ],
        },
    )
