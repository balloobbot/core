"""Configure The Things Stack with explicit application-scoped access."""

from collections.abc import Mapping
from typing import Any, override

from lorawan_connection import ConnectionUnavailable
from lorawan_connection.backend.tts import AuthenticationError, TTSConnection
import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import CONF_APPLICATION_IDS, CONF_ENDPOINT, CONF_IDENTITY_SERVER, DOMAIN


class TheThingsStackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure application and identity endpoints without requiring a user key."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize pending server input."""
        self._input: dict[str, Any] = {}

    async def _validate(self) -> str | None:
        try:
            connection = TTSConnection(
                self._input[CONF_ENDPOINT],
                self._input[CONF_API_KEY],
                identity_server=self._input.get(CONF_IDENTITY_SERVER),
                application_ids=self._input[CONF_APPLICATION_IDS],
                network_id="validation",
            )
        except ValueError:
            return "invalid_endpoint"
        try:
            await connection.async_connect()
        except AuthenticationError:
            return "invalid_auth"
        except ConnectionUnavailable:
            return "cannot_connect"
        finally:
            await connection.close()
        return None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate both endpoints and all selected applications."""
        errors = {}
        if user_input is not None:
            self._input = dict(user_input)
            self._input[CONF_APPLICATION_IDS] = sorted(
                {app.strip() for app in user_input[CONF_APPLICATION_IDS] if app.strip()}
            )
            if not self._input[CONF_APPLICATION_IDS]:
                errors[CONF_APPLICATION_IDS] = "select_application"
            elif error := await self._validate():
                errors["base"] = error
            else:
                endpoint = self._input[CONF_ENDPOINT].rstrip("/").lower()
                identity = (
                    (self._input.get(CONF_IDENTITY_SERVER) or endpoint)
                    .rstrip("/")
                    .lower()
                )
                await self.async_set_unique_id(
                    f"{endpoint}|{identity}|{','.join(self._input[CONF_APPLICATION_IDS])}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="The Things Stack", data=self._input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ENDPOINT): str,
                    probatio.Optional(CONF_IDENTITY_SERVER): str,
                    probatio.Required(CONF_APPLICATION_IDS): SelectSelector(
                        SelectSelectorConfig(
                            options=[], multiple=True, custom_value=True
                        )
                    ),
                    probatio.Required(probatio.Secret(CONF_API_KEY)): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Keep server scope and entity identity when replacing credentials."""
        self._input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials before storing them."""
        errors = {}
        if user_input is not None:
            self._input.update(user_input)
            if error := await self._validate():
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required(probatio.Secret(CONF_API_KEY)): str,
                }
            ),
            errors=errors,
        )
