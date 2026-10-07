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
    """Return retained device status, support reasons, and provider availability."""
    registry = hass.data[DATA_REGISTRY]
    collection = registry.inventories.get(msg["entry_id"])
    if collection is None:
        connection.send_error(
            msg["id"], "not_found", "LoRaWAN connection is not registered"
        )
        return
    available = msg["entry_id"] in registry.connections
    devices = [
        (
            device.descriptor,
            {
                "received_at": status.received_at.isoformat(),
                "battery_level": device.battery_level,
                "external_power_source": device.external_power_source,
                "downlink_margin": device.downlink_margin,
            }
            if (status := device.latest_status) is not None
            else None,
        )
        for device in collection.devices.values()
    ]
    connection.send_result(
        msg["id"],
        {
            "available": available,
            "devices": [
                {
                    **asdict(device),
                    "status": status,
                    "unsupported_reason": await async_unsupported_reason(
                        hass, device, registry.integrations
                    ),
                }
                for device, status in devices
            ],
        },
    )
