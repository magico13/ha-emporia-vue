"""Constants for the Emporia Vue integration."""

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

DOMAIN = "emporia_vue"
VUE_DATA = "vue_data"
ENABLE_1S = "enable_1s"
ENABLE_1M = "enable_1m"
ENABLE_1D = "enable_1d"
ENABLE_1MON = "enable_1mon"

DOMAIN_SCHEMA = vol.Schema(
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
        DOMAIN: DOMAIN_SCHEMA,
    },
    extra=vol.ALLOW_EXTRA,
)
