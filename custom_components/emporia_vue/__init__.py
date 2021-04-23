"""The Emporia Vue integration."""
import asyncio
from datetime import datetime, timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

import logging
from pyemvue.enums import Scale

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from pyemvue import PyEmVue

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
                vol.Optional(ENABLE_1MON, default=True): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]


device_gids = []
device_information = []


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
                ENABLE_1MON: conf[ENABLE_1MON],
            },
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Emporia Vue from a config entry."""
    global device_gids
    global device_information
    device_gids = []
    device_information = []

    entry_data = entry.data
    email = entry_data[CONF_EMAIL]
    password = entry_data[CONF_PASSWORD]
    # _LOGGER.info(entry_data)
    vue = PyEmVue()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, vue.login, email, password)
        if not result:
            raise Exception("Could not authenticate with Emporia API")
    except Exception:
        _LOGGER.error("Could not authenticate with Emporia API")
        return False

    scales_1m = []
    scales_1s = []
    try:
        devices = await loop.run_in_executor(None, vue.get_devices)
        total_channels = 0
        for d in devices:
            total_channels += len(d.channels)
        _LOGGER.warn(
            "Found {0} Emporia devices with {1} total channels".format(
                len(devices), total_channels
            )
        )
        for device in devices:
            if not device.device_gid in device_gids:
                device_gids.append(device.device_gid)
            await loop.run_in_executor(None, vue.populate_device_properties, device)
            device_information.append(device)

        async def async_update_data_1min():
            """Fetch data from API endpoint at a 1 minute interval

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            return await update_sensors(vue, scales_1m)

        async def async_update_data_1second():
            """Fetch data from API endpoint at a 1 second interval

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            return await update_sensors(vue, scales_1s)

        if ENABLE_1M not in entry_data or entry_data[ENABLE_1M]:
            scales_1m.append(Scale.MINUTE.value)
        if ENABLE_1D not in entry_data or entry_data[ENABLE_1D]:
            scales_1m.append(Scale.DAY.value)
        if ENABLE_1MON not in entry_data or entry_data[ENABLE_1MON]:
            scales_1m.append(Scale.MONTH.value)

        coordinator_1min = None
        if scales_1m:
            coordinator_1min = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor",
                update_method=async_update_data_1min,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(seconds=60),
            )
            await coordinator_1min.async_config_entry_first_refresh()
            _LOGGER.warn(f"1min Update data: {coordinator_1min.data}")
        coordinator_1s = None
        if ENABLE_1S in entry_data and entry_data[ENABLE_1S]:
            scales_1s.append(Scale.SECOND.value)
            coordinator_1s = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor1s",
                update_method=async_update_data_1second,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(seconds=1),
            )
            await coordinator_1s.async_config_entry_first_refresh()
            _LOGGER.warn(f"1s Update data: {coordinator_1s.data}")
    except Exception as err:
        _LOGGER.warn(f"Exception while setting up Emporia Vue. Will retry. {err}")
        raise ConfigEntryNotReady(
            f"Exception while setting up Emporia Vue. Will retry. {err}"
        )

    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: vue,
        "coordinator_1min": coordinator_1min,
        "coordinator_1s": coordinator_1s,
    }

    try:
        for component in PLATFORMS:
            hass.async_create_task(
                hass.config_entries.async_forward_entry_setup(entry, component)
            )
    except Exception as err:
        _LOGGER.warn(f"Error setting up platforms: {err}")
        raise ConfigEntryNotReady(f"Error setting up platforms: {err}")

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


async def update_sensors(vue, scales):
    try:
        # Note: asyncio.TimeoutError and aiohttp.ClientError are already
        # handled by the data update coordinator.
        data = {}
        loop = asyncio.get_event_loop()
        for scale in scales:
            now = datetime.utcnow() - timedelta(seconds=1)
            channels = await loop.run_in_executor(
                None, vue.get_devices_usage, device_gids, now, scale
            )
            if not channels:
                _LOGGER.warn(
                    f"No channels found during update for scale {scale}. Retrying..."
                )
                channels = await loop.run_in_executor(
                    None, vue.get_devices_usage, device_gids, now, scale
                )
            if channels:
                for channel in channels:
                    id = "{0}-{1}-{2}".format(
                        channel.device_gid, channel.channel_num, scale
                    )
                    usage = round(channel.usage, 3)
                    if scale == Scale.MINUTE.value:
                        usage = round(
                            60 * 1000 * channel.usage
                        )  # convert from kwh to w rate
                    elif scale == Scale.SECOND.value:
                        usage = round(3600 * 1000 * channel.usage)  # convert to rate
                    elif scale == Scale.MINUTES_15.value:
                        usage = round(
                            4 * 1000 * channel.usage
                        )  # this might never be used but for safety, convert to rate
                    info = None
                    for device in device_information:
                        if device.device_gid == channel.device_gid:
                            for channel2 in device.channels:
                                if channel2.channel_num == channel.channel_num:
                                    info = device
                                    break

                    data[id] = {
                        "device_gid": channel.device_gid,
                        "channel_num": channel.channel_num,
                        "usage": usage,
                        "scale": scale,
                        "info": info,
                    }
            else:
                _LOGGER.warn(f"No channels found during update for scale {scale}")

        return data
    except Exception as err:
        _LOGGER.error(f"Error communicating with Emporia API: {err}")
        raise UpdateFailed(f"Error communicating with Emporia API: {err}")