"""Configure one Dragino collection per LoRaWAN network."""

from typing import Any, override

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import ConfigEntrySelector

from . import DOMAIN


class DraginoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover collections through the LoRaWAN provider."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize discovery context."""
        self._discovery: dict[str, Any] = {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a provider, skipping the chooser for a single network."""
        providers = {
            entry.entry_id: entry
            for entry in self.hass.config_entries.async_entries(
                "lorawan", include_disabled=False
            )
        }
        if not providers:
            return self.async_abort(reason="no_provider")

        errors = {}
        if user_input is None and len(providers) == 1:
            user_input = {"connection_entry_id": next(iter(providers))}
        if user_input is not None:
            if provider := providers.get(user_input["connection_entry_id"]):
                return await self.async_step_integration_discovery(
                    {
                        "connection_entry_id": provider.entry_id,
                        "network_id": provider.data["network_id"],
                    }
                )
            errors["base"] = "invalid_provider"

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required("connection_entry_id"): ConfigEntrySelector(
                        {"integration": "lorawan"}
                    )
                }
            ),
            errors=errors,
        )

    @override
    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Deduplicate the entire collection, not individual devices."""
        await self.async_set_unique_id(discovery_info["network_id"])
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding supported Dragino devices to HA."""
        if user_input is not None:
            return self.async_create_entry(title="Dragino", data=self._discovery)
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm")
