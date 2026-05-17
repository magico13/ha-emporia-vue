"""Constants for the Emporia Vue integration."""

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

DOMAIN = "emporia_vue"
VUE_DATA = "vue_data"
AUTH_METHOD = "auth_method"
AUTH_METHOD_EMAIL_PASSWORD = "email_password"
AUTH_METHOD_TOKENS = "tokens"
CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
ENABLE_1S = "enable_1s"
ENABLE_1M = "enable_1m"
ENABLE_1D = "enable_1d"
ENABLE_1MON = "enable_1mon"
SOLAR_INVERT = "solar_invert"
CUSTOMER_GID = "customer_gid"
CONFIG_TITLE = "title"

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
        vol.Required(CONF_PASSWORD): cv.string,
        **CONFIG_OPTIONS_SCHEMA,
    }
)

TOKEN_CONFIG_FLOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID_TOKEN): cv.string,
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        **CONFIG_OPTIONS_SCHEMA,
    }
)
