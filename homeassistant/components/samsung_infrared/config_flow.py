"""Config flow for Samsung IR integration."""

from typing import Any, override

import probatio

from homeassistant.components.infrared import InfraredConsumerConfigFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DEVICE_TYPE,
    CONF_INFRARED_EMITTER_ENTITY_ID,
    DOMAIN,
    SamsungDeviceType,
)


class SamsungIrConfigFlow(InfraredConsumerConfigFlow, domain=DOMAIN):
    """Handle config flow for Samsung IR."""

    VERSION = 1
    INFRARED_EMITTER_KEY = CONF_INFRARED_EMITTER_ENTITY_ID

    @override
    @callback
    def _async_infrared_schema(self) -> probatio.Schema:
        """Return the device type and emitter selection schema."""
        return probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            device_type.value for device_type in SamsungDeviceType
                        ],
                        translation_key=CONF_DEVICE_TYPE,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                **self._async_infrared_entity_fields(),
            }
        )

    @override
    @callback
    def _async_infrared_match_dict(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Scope duplicate checks to the device type."""
        return {CONF_DEVICE_TYPE: user_input[CONF_DEVICE_TYPE]}

    @override
    async def _async_infrared_entry_title(self, user_input: dict[str, Any]) -> str:
        """Return the title of a new entry."""
        entity_id = user_input[CONF_INFRARED_EMITTER_ENTITY_ID]
        entry = er.async_get(self.hass).async_get(entity_id)
        entity_name = (
            entry.name or entry.original_name or entity_id if entry else entity_id
        )
        device_type = SamsungDeviceType(user_input[CONF_DEVICE_TYPE])
        return f"Samsung {device_type.name} via {entity_name}"
