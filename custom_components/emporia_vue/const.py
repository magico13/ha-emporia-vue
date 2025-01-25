"""Constants for the Emporia Vue integration."""

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

DOMAIN = "emporia_vue"
VUE_DATA = "vue_data"
ENABLE_1S = "enable_1s"
ENABLE_1M = "enable_1m"
ENABLE_1D = "enable_1d"
ENABLE_1MON = "enable_1mon"
CUSTOMER_GID = "customer_gid"
CONFIG_TITLE = "title"

USER_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(ENABLE_1M, default=True): cv.boolean,  # type: ignore
        vol.Optional(ENABLE_1D, default=True): cv.boolean,  # type: ignore
        vol.Optional(ENABLE_1MON, default=True): cv.boolean,  # type: ignore
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(ENABLE_1M, default=True): cv.boolean,  # type: ignore
                vol.Optional(ENABLE_1D, default=True): cv.boolean,  # type: ignore
                vol.Optional(ENABLE_1MON, default=True): cv.boolean,  # type: ignore
                vol.Required(CUSTOMER_GID): cv.positive_int,
                vol.Required(CONFIG_TITLE): cv.string,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)
