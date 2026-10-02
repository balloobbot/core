"""WebSocket commands for integrations installed from a ZIP archive."""

from dataclasses import asdict
from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from ..base import MarketplaceManager
from ..exceptions import MarketplaceError, ReplacesBuiltInNotConfirmedError
from .decorators import (
    marketplace_command,
    send_marketplace_error,
    send_translated_error,
)


@websocket_api.websocket_command(
    {probatio.Required("type"): "marketplace/archives/list"}
)
@websocket_api.require_admin
@websocket_api.async_response
@marketplace_command()
async def marketplace_archives_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    marketplace: MarketplaceManager,
) -> None:
    """List the integrations installed from an archive."""
    connection.send_message(
        websocket_api.result_message(
            msg["id"],
            [
                asdict(integration)
                for integration in marketplace.archives.list_installed
            ],
        )
    )


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "marketplace/archive/install",
        probatio.Required("file_id"): cv.string,
        probatio.Optional("confirm_replace_built_in", default=False): cv.boolean,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@marketplace_command(requires_accepted_warning=True)
async def marketplace_archive_install(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    marketplace: MarketplaceManager,
) -> None:
    """Install the integration of an uploaded archive, or update it."""
    try:
        integration = await marketplace.archives.async_install(
            msg["file_id"], confirm_replace_built_in=msg["confirm_replace_built_in"]
        )
    except ReplacesBuiltInNotConfirmedError as exception:
        send_translated_error(
            connection,
            msg["id"],
            "replaces_built_in",
            "replaces_built_in_not_confirmed",
            exception.translation_placeholders,
        )
        return
    except MarketplaceError as exception:
        send_marketplace_error(
            connection, msg["id"], "error", exception, "archive_install_failed", {}
        )
        return

    connection.send_message(
        websocket_api.result_message(msg["id"], asdict(integration))
    )


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "marketplace/archive/uninstall",
        probatio.Required("domain"): cv.string,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@marketplace_command()
async def marketplace_archive_uninstall(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    marketplace: MarketplaceManager,
) -> None:
    """Uninstall an integration that an archive installed."""
    domain = msg["domain"]
    if (integration := marketplace.archives.get(domain)) is None:
        send_translated_error(
            connection,
            msg["id"],
            "archive_not_installed",
            "archive_not_installed",
            {"domain": domain},
        )
        return

    # Its config entries run the installed code, ignored ones too, they go first
    if hass.config_entries.async_entries(domain):
        send_translated_error(
            connection,
            msg["id"],
            "repository_in_use",
            "repository_in_use",
            {"repository": integration.name},
        )
        return

    try:
        await marketplace.archives.async_uninstall(domain)
    except OSError:
        send_translated_error(
            connection,
            msg["id"],
            "uninstall_failed",
            "uninstall_failed",
            {"repository": integration.name},
        )
        return

    connection.send_message(websocket_api.result_message(msg["id"]))
