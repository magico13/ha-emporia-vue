"""Constants for the Emporia Vue integration."""

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

DOMAIN = "emporia_vue"
AUTH_METHOD = "auth_method"
AUTH_METHOD_EMAIL_PASSWORD = "email_password"
AUTH_METHOD_TOKENS = "tokens"
CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
ENABLE_1M = "enable_1m"
ENABLE_1D = "enable_1d"
ENABLE_1MON = "enable_1mon"
SOLAR_INVERT = "solar_invert"
CUSTOMER_GID = "customer_gid"
CONFIG_TITLE = "title"

# Used for password/token fields so they render as masked inputs in the
# config flow UI instead of plain text boxes.
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

AUTH_METHOD_SCHEMA = vol.Schema(
    {
        vol.Required(AUTH_METHOD, default=AUTH_METHOD_EMAIL_PASSWORD): vol.In(
            {
                AUTH_METHOD_EMAIL_PASSWORD: "Emporia email and password",
                AUTH_METHOD_TOKENS: "Emporia tokens (Google/SSO accounts)",
            }
        ),
    }
)

CONFIG_OPTIONS_SCHEMA = {
    vol.Optional(ENABLE_1M, default=True): cv.boolean,
    vol.Optional(ENABLE_1D, default=True): cv.boolean,
    vol.Optional(ENABLE_1MON, default=True): cv.boolean,
    vol.Optional(SOLAR_INVERT, default=True): cv.boolean,
}

CONFIG_FLOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        **CONFIG_OPTIONS_SCHEMA,
    }
)

TOKEN_CONFIG_FLOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID_TOKEN): PASSWORD_SELECTOR,
        vol.Required(CONF_ACCESS_TOKEN): PASSWORD_SELECTOR,
        vol.Required(CONF_REFRESH_TOKEN): PASSWORD_SELECTOR,
        **CONFIG_OPTIONS_SCHEMA,
    }
)
