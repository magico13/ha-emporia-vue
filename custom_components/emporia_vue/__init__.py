"""The Emporia Vue integration."""
import asyncio
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from pyemvue import PyEmVue
from pyemvue.device import VueDevice, VueDeviceChannel

from .const import DOMAIN, VUE_DATA, ENABLE_1S, ENABLE_1M, ENABLE_1D, ENABLE_1MON

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(ENABLE_1S, default=False): cv.boolean,
                vol.Optional(ENABLE_1M, default=True): cv.boolean,
                vol.Optional(ENABLE_1D, default=True): cv.boolean,
                vol.Optional(ENABLE_1MON, default=True): cv.boolean
            }
        )
    }, 
    extra=vol.ALLOW_EXTRA
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Emporia Vue component."""
    hass.data.setdefault(DOMAIN, {})
    conf = config.get(DOMAIN)
    if not conf:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_EMAIL: conf[CONF_EMAIL],
                CONF_PASSWORD: conf[CONF_PASSWORD],
                ENABLE_1S: conf[ENABLE_1S],
                ENABLE_1M: conf[ENABLE_1M],
                ENABLE_1D: conf[ENABLE_1D],
                ENABLE_1MON: conf[ENABLE_1MON]
            },
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Emporia Vue from a config entry."""
    entry_data = entry.data
    email = entry_data[CONF_EMAIL]
    password = entry_data[CONF_PASSWORD]
    #_LOGGER.info(entry_data)
    vue = PyEmVue()
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, vue.login, email, password)
        if not result:
            raise Exception("Could not authenticate with Emporia API")
    except Exception:
        _LOGGER.error("Could not authenticate with Emporia API")
        return False

    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: vue,
        ENABLE_1S: False if ENABLE_1S not in entry_data else entry_data[ENABLE_1S],
        ENABLE_1M: True if ENABLE_1M not in entry_data[ENABLE_1M] else entry_data[ENABLE_1M],
        ENABLE_1D: True if ENABLE_1D not in entry_data else entry_data[ENABLE_1D],
        ENABLE_1MON: True if ENABLE_1MON not in entry_data else entry_data[ENABLE_1MON]
    }

    for component in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )
        
    return True




async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
