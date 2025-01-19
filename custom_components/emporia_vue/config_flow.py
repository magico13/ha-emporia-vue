"""Config flow for Emporia Vue integration."""

import asyncio
import logging
from typing import Any

from pyemvue import PyEmVue
import voluptuous as vol

from homeassistant import config_entries, core, exceptions
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .const import DOMAIN, DOMAIN_SCHEMA, ENABLE_1D, ENABLE_1M, ENABLE_1MON

_LOGGER: logging.Logger = logging.getLogger(__name__)


class VueHub:
    """Hub for the Emporia Vue Integration."""

    def __init__(self) -> None:
        """Initialize."""
        self.vue = PyEmVue()

    async def authenticate(self, username, password) -> bool:
        """Test if we can authenticate with the host."""
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.vue.login, username, password)


async def validate_input(data: dict):
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

    # Return info that you want to store in the config entry.
    return {
        "title": f"Customer {hub.vue.customer.customer_gid}",
        "gid": f"{hub.vue.customer.customer_gid}",
        ENABLE_1M: data[ENABLE_1M],
        ENABLE_1D: data[ENABLE_1D],
        ENABLE_1MON: data[ENABLE_1MON],
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emporia Vue."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(user_input)
                # prevent setting up the same account twice
                await self.async_set_unique_id(info["gid"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"], data=user_input, options=user_input
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DOMAIN_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Perform reauthentication upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Confirm reauthentication dialog."""
        errors: dict[str, str] = {}
        if user_input:
            gid = 0
            try:
                hub = VueHub()
                if not await hub.authenticate(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                ):
                    raise InvalidAuth
                gid = hub.vue.customer.customer_gid
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(str(gid))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @core.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Emporia Vue."""

    async def async_step_init(self, user_input=None) -> config_entries.FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        ENABLE_1M,
                        default=self.config_entry.options.get(ENABLE_1M, True),
                    ): bool,
                    vol.Optional(
                        ENABLE_1D,
                        default=self.config_entry.options.get(ENABLE_1D, True),
                    ): bool,
                    vol.Optional(
                        ENABLE_1MON,
                        default=self.config_entry.options.get(ENABLE_1MON, True),
                    ): bool,
                }
            ),
        )


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
