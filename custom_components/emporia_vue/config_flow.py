"""Config flow for Emporia Vue integration."""

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from pyemvue import PyEmVue
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

from .const import (
    CONFIG_FLOW_SCHEMA,
    CONFIG_TITLE,
    CUSTOMER_GID,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


class VueHub:
    """Hub for the Emporia Vue Integration."""

    def __init__(self) -> None:
        """Initialize."""
        self.vue = PyEmVue()

    async def authenticate(self, username, password) -> bool:
        """Test if we can authenticate with the host."""
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        # support using the simulator by looking at the username
        # if formatted like vue_simulator@localhost:8000 then use the simulator
        if username.startswith("vue_simulator@"):
            host = username.split("@")[1]
            return await loop.run_in_executor(None, self.vue.login_simulator, host)
        return await loop.run_in_executor(None, self.vue.login, username, password)


async def validate_input(data: dict | Mapping[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    hub = VueHub()
    if not await hub.authenticate(data[CONF_EMAIL], data[CONF_PASSWORD]):
        raise InvalidAuth

    # If you cannot connect:
    # throw CannotConnect
    # If the authentication is wrong:
    # InvalidAuth

    if not hub.vue.customer:
        raise InvalidAuth

    new_data = dict(data)

    if SOLAR_INVERT not in new_data:
        new_data[SOLAR_INVERT] = True

    # Return info that you want to store in the config entry.
    return {
        CONFIG_TITLE: f"{hub.vue.customer.email} ({hub.vue.customer.customer_gid})",
        CUSTOMER_GID: f"{hub.vue.customer.customer_gid}",
        ENABLE_1M: new_data[ENABLE_1M],
        ENABLE_1D: new_data[ENABLE_1D],
        ENABLE_1MON: new_data[ENABLE_1MON],
        SOLAR_INVERT: new_data[SOLAR_INVERT],
        CONF_EMAIL: new_data[CONF_EMAIL],
        CONF_PASSWORD: new_data[CONF_PASSWORD],
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emporia Vue."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(user_input)
                # prevent setting up the same account twice
                await self.async_set_unique_id(info[CUSTOMER_GID])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info[CONFIG_TITLE], data=user_input
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=CONFIG_FLOW_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the reconfiguration step."""
        current_config = self._get_reconfigure_entry()
        if user_input is not None:
            _LOGGER.debug("User input on reconfigure was the following: %s", user_input)
            _LOGGER.debug("Current config is: %s", current_config.data)
            info = current_config.data
            # if gid is not in current config, reauth and get gid again
            if (
                CUSTOMER_GID not in current_config.data
                or not current_config.data[CUSTOMER_GID]
            ):
                info = await validate_input(current_config.data)

            await self.async_set_unique_id(info[CUSTOMER_GID])
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            data = {
                ENABLE_1M: user_input[ENABLE_1M],
                ENABLE_1D: user_input[ENABLE_1D],
                ENABLE_1MON: user_input[ENABLE_1MON],
                SOLAR_INVERT: user_input[SOLAR_INVERT],
                CUSTOMER_GID: info[CUSTOMER_GID],
                CONFIG_TITLE: info[CONFIG_TITLE],
            }
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates=data,
            )

        data_schema: dict[vol.Optional | vol.Required, Any] = {
            vol.Optional(
                ENABLE_1M,
                default=current_config.data.get(ENABLE_1M, True),
            ): cv.boolean,
            vol.Optional(
                ENABLE_1D,
                default=current_config.data.get(ENABLE_1D, True),
            ): cv.boolean,
            vol.Optional(
                ENABLE_1MON,
                default=current_config.data.get(ENABLE_1MON, True),
            ): cv.boolean,
            vol.Optional(
                SOLAR_INVERT,
                default=current_config.data.get(SOLAR_INVERT, True),
            ): cv.boolean,
        }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(data_schema),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Perform reauthentication upon an API authentication error."""
        return await self.async_step_reauth_confirm(entry_data)

    async def async_step_reauth_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm reauthentication dialog."""
        errors: dict[str, str] = {}
        existing_entry = self._get_reauth_entry()
        if user_input:
            gid = 0
            try:
                hub = VueHub()
                if (
                    not await hub.authenticate(
                        user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                    )
                    or not hub.vue.customer
                ):
                    raise InvalidAuth
                gid = hub.vue.customer.customer_gid
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(str(gid))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    existing_entry,
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=existing_entry.data[CONF_EMAIL]
                    ): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                }
            ),
            errors=errors,
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
