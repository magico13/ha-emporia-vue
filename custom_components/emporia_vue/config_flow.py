"""Config flow for Emporia Vue integration."""

import asyncio
import logging

from pyemvue import PyEmVue
import voluptuous as vol

from homeassistant import config_entries, core, exceptions
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .const import DOMAIN, ENABLE_1S, ENABLE_1D, ENABLE_1M, ENABLE_1MON

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(ENABLE_1S, default=False): bool,
        vol.Optional(ENABLE_1M, default=True): bool,
        vol.Optional(ENABLE_1D, default=True): bool,
        vol.Optional(ENABLE_1MON, default=True): bool,
    }
)


class VueHub:
    """Hub for the Emporia Vue Integration."""

    def __init__(self):
        """Initialize."""
        self.vue = PyEmVue()
        pass

    async def authenticate(self, username, password) -> bool:
        """Test if we can authenticate with the host."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.vue.login, username, password)
        return result


async def validate_input(hass: core.HomeAssistant, data):
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

    # Return info that you want to store in the config entry.
    return {
        "title": f"Customer {hub.vue.customer.customer_gid}",
        "gid": f"{hub.vue.customer.customer_gid}",
        ENABLE_1S: data[ENABLE_1S],
        ENABLE_1M: data[ENABLE_1M],
        ENABLE_1D: data[ENABLE_1D],
        ENABLE_1MON: data[ENABLE_1MON],
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emporia Vue."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                # prevent setting up the same account twice
                await self.async_set_unique_id(info["gid"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                    options=user_input,
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
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


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a options flow for Emporia Vue."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        ENABLE_1S, default=self.config_entry.options.get(ENABLE_1S)
                    ): bool,
                    vol.Optional(
                        ENABLE_1M, default=self.config_entry.options.get(ENABLE_1M)
                    ): bool,
                    vol.Optional(
                        ENABLE_1D, default=self.config_entry.options.get(ENABLE_1D)
                    ): bool,
                    vol.Optional(
                        ENABLE_1MON, default=self.config_entry.options.get(ENABLE_1MON)
                    ): bool,
                }
            ),
        )
