"""Configure an external ChirpStack network."""

from collections.abc import Mapping
from typing import Any, override
from uuid import uuid4

import grpc
import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from ._vendor.chirpstack_connection import ChirpStackConnection, ConnectionUnavailable
from .const import (
    CONF_APPLICATION_IDS,
    CONF_ENDPOINT,
    CONF_NETWORK_ID,
    CONF_TENANT_ID,
    DOMAIN,
)


class LoRaWANConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover tenants when the API key allows it; select applications."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize pending input."""
        self._input: dict[str, Any] = {}
        self._applications: dict[str, str] = {}
        self._tenants: dict[str, str] = {}

    def _connection(self) -> ChirpStackConnection:
        return ChirpStackConnection(
            self._input[CONF_ENDPOINT],
            self._input[CONF_API_KEY],
            self._input.get(CONF_TENANT_ID, ""),
            [],
            "validation",
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect with explicitly selected transport security."""
        errors = {}
        if user_input is not None:
            self._input = dict(user_input)
            try:
                connection = self._connection()
            except ValueError:
                errors["base"] = "invalid_endpoint"
            else:
                try:
                    self._tenants = await connection.tenants()
                except grpc.aio.AioRpcError as error:
                    # ChirpStack also uses UNAUTHENTICATED for insufficient scope.
                    if error.code() not in (
                        grpc.StatusCode.UNAUTHENTICATED,
                        grpc.StatusCode.PERMISSION_DENIED,
                    ):
                        errors["base"] = "cannot_connect"
                finally:
                    await connection.close()
                if not errors:
                    if len(self._tenants) == 1:
                        self._input[CONF_TENANT_ID] = next(iter(self._tenants))
                        return await self.async_step_tenant(
                            {CONF_TENANT_ID: self._input[CONF_TENANT_ID]}
                        )
                    return await self.async_step_tenant()
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_ENDPOINT, default="https://chirpstack.local:8080"
                    ): str,
                    probatio.Required(probatio.Secret(CONF_API_KEY)): str,
                }
            ),
            errors=errors,
        )

    async def async_step_tenant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List applications after validating access to the selected tenant."""
        errors = {}
        if user_input is not None:
            self._input.update(user_input)
            connection = self._connection()
            try:
                self._applications = await connection.applications()
            except grpc.aio.AioRpcError as error:
                errors["base"] = (
                    "invalid_auth"
                    if error.code() == grpc.StatusCode.UNAUTHENTICATED
                    else "cannot_connect"
                )
            except ConnectionUnavailable:
                errors["base"] = "cannot_connect"
            finally:
                await connection.close()
            if not errors:
                await self.async_set_unique_id(
                    f"{self._input[CONF_ENDPOINT].rstrip('/').lower()}:{self._input[CONF_TENANT_ID]}"
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_applications()
        selector = (
            SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=key, label=name)
                        for key, name in self._tenants.items()
                    ]
                )
            )
            if self._tenants
            else str
        )
        return self.async_show_form(
            step_id="tenant",
            data_schema=probatio.Schema({probatio.Required(CONF_TENANT_ID): selector}),
            errors=errors,
        )

    async def async_step_applications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose an explicit application scope."""
        if user_input is not None:
            self._input.update(user_input)
            self._input[CONF_NETWORK_ID] = str(uuid4())
            return self.async_create_entry(
                title=self._tenants.get(self._input[CONF_TENANT_ID], "LoRaWAN"),
                data=self._input,
            )
        return self.async_show_form(
            step_id="applications",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_APPLICATION_IDS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=name)
                                for key, name in self._applications.items()
                            ],
                            multiple=True,
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Replace rejected credentials without changing network identity."""
        self._input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the replacement key against the existing scope."""
        errors = {}
        if user_input is not None:
            self._input.update(user_input)
            connection = self._connection()
            try:
                applications = await connection.applications()
                if not set(self._input[CONF_APPLICATION_IDS]) <= applications.keys():
                    errors["base"] = "cannot_connect"
            except grpc.aio.AioRpcError as error:
                errors["base"] = (
                    "invalid_auth"
                    if error.code() == grpc.StatusCode.UNAUTHENTICATED
                    else "cannot_connect"
                )
            except ConnectionUnavailable:
                errors["base"] = "cannot_connect"
            finally:
                await connection.close()
            if not errors:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {probatio.Required(probatio.Secret(CONF_API_KEY)): str}
            ),
            errors=errors,
        )
