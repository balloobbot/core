"""Admin-only inventory for a future LoRaWAN UI."""

from dataclasses import asdict
from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
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
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        connection.send_error(msg["id"], "not_found", "LoRaWAN network is not loaded")
        return
    runtime = entry.runtime_data
    connection.send_result(
        msg["id"],
        {
            "available": runtime.connection.available,
            "devices": [
                {
                    **asdict(device),
                    "unsupported_reason": await async_unsupported_reason(
                        hass, device, runtime.integrations
                    ),
                }
                for device in tuple(runtime.connection.devices.values())
            ],
        },
    )
