"""Confirm one SenseCAP collection per discovered LoRaWAN network."""

from typing import Any, override

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from . import DOMAIN


class SenseCapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover collections through the LoRaWAN provider."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize discovery context."""
        self._discovery: dict[str, Any] = {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Direct users to the provider for the initial release."""
        return self.async_abort(reason="discovery_only")

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
        """Confirm adding supported SenseCAP devices to HA."""
        if user_input is not None:
            return self.async_create_entry(title="SenseCAP", data=self._discovery)
        return self.async_show_form(step_id="confirm")
