"""The Emporia Vue integration."""
import asyncio
from datetime import datetime, timedelta, timezone
import dateutil
import logging

from pyemvue import PyEmVue
from pyemvue.device import VueDeviceChannel
from pyemvue.enums import Scale
import re

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, VUE_DATA, ENABLE_1M, ENABLE_1D, ENABLE_1MON

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
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
device_information = {}
last_minute_data = {}
last_day_data = {}
last_day_update = None


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
    device_information = {}

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

    try:
        devices = await loop.run_in_executor(None, vue.get_devices)
        total_channels = 0
        for d in devices:
            total_channels += len(d.channels)
        _LOGGER.info(
            "Found {0} Emporia devices with {1} total channels".format(
                len(devices), total_channels
            )
        )
        for device in devices:
            if not device.device_gid in device_gids:
                device_gids.append(device.device_gid)
                # await loop.run_in_executor(None, vue.populate_device_properties, device)
                device_information[device.device_gid] = device
            else:
                device_information[device.device_gid].channels += device.channels

        async def async_update_data_1min():
            """Fetch data from API endpoint at a 1 minute interval

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            global last_minute_data
            data = await update_sensors(vue, [Scale.MINUTE.value])
            # store this, then have the daily sensors pull from it and integrate
            # then the daily can "true up" hourly (or more frequent) in case it's incorrect
            if data:
                last_minute_data = data
            return data

        async def async_update_data_1hr():
            """Fetch data from API endpoint at a 1 hour interval

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            return await update_sensors(vue, [Scale.MONTH.value])

        async def async_update_day_sensors():
            global last_day_update
            global last_day_data
            now = datetime.now(timezone.utc)
            if not last_day_update or (now - last_day_update) > timedelta(minutes=15):
                _LOGGER.info("Updating day sensors")
                last_day_update = now
                last_day_data = await update_sensors(vue, [Scale.DAY.value])
            else:
                # integrate the minute data
                _LOGGER.info("Integrating minute data into day sensors")
                if last_minute_data:
                    for identifier, data in last_minute_data.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        day_id = f"{device_gid}-{channel_gid}-{Scale.DAY.value}"
                        if (
                            data
                            and last_day_data
                            and day_id in last_day_data
                            and last_day_data[day_id]
                            and "usage" in last_day_data[day_id]
                            and last_day_data[day_id]["usage"] is not None
                        ):
                            # if we just passed midnight, then reset back to zero
                            handle_midnight(now, int(device_gid), day_id)

                            last_day_data[day_id]["usage"] += data[
                                "usage"
                            ]  # already in kwh
            return last_day_data

        coordinator_1min = None
        if ENABLE_1M not in entry_data or entry_data[ENABLE_1M]:
            coordinator_1min = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor",
                update_method=async_update_data_1min,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(minutes=1),
            )
            await coordinator_1min.async_config_entry_first_refresh()
            _LOGGER.info("1min Update data: %s", coordinator_1min.data)
        coordinator_1hr = None
        if ENABLE_1MON not in entry_data or entry_data[ENABLE_1MON]:
            coordinator_1hr = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor",
                update_method=async_update_data_1hr,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(hours=1),
            )
            await coordinator_1hr.async_config_entry_first_refresh()
            _LOGGER.info("1hr Update data: %s", coordinator_1hr.data)

        coordinator_day_sensor = None
        if ENABLE_1D not in entry_data or entry_data[ENABLE_1D]:
            coordinator_day_sensor = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor",
                update_method=async_update_day_sensors,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(minutes=1),
            )
            await coordinator_day_sensor.async_config_entry_first_refresh()

        # Setup custom services
        async def handle_set_charger_current(call):
            """Handle setting the EV Charger current"""
            _LOGGER.debug(
                "executing set_charger_current: %s %s",
                str(call.service),
                str(call.data),
            )
            current = call.data.get("current")
            device_id = call.data.get("device_id", None)
            entity_id = call.data.get("entity_id", None)

            charger_entity = None
            if device_id:
                entity_registry = er.async_get(hass)
                entities = er.async_entries_for_device(entity_registry, device_id[0])
                for entity in entities:
                    _LOGGER.info("Entity is %s", str(entity))
                    if entity.entity_id.startswith("switch"):
                        charger_entity = entity
                        break
                if not charger_entity:
                    charger_entity = entities[0]
            elif entity_id:
                entity_registry = er.async_get(hass)
                charger_entity = entity_registry.async_get(entity_id[0])
            else:
                raise HomeAssistantError("Target device or Entity required.")

            unique_entity_id = charger_entity.unique_id
            gid_match = re.search(r"\d+", unique_entity_id)
            if not gid_match:
                raise HomeAssistantError(
                    f"Could not find device gid from unique id {unique_entity_id}"
                )

            charger_gid = int(gid_match.group(0))
            if (
                charger_gid not in device_information
                or not device_information[charger_gid].ev_charger
            ):
                raise HomeAssistantError(
                    f"Set Charging Current called on invalid device with entity id {charger_entity.entity_id} (unique id {unique_entity_id})"
                )

            charger_info = device_information[charger_gid]
            # Scale the current to a minimum of 6 amps and max of the circuit max
            current = max(6, current)
            current = min(current, charger_info.ev_charger.max_charging_rate)
            _LOGGER.info(
                "Setting charger %s to current of %d amps", charger_gid, current
            )

            updated_charger = await loop.run_in_executor(
                None, vue.update_charger, charger_info.ev_charger, None, current
            )
            device_information[charger_gid].ev_charger = updated_charger

        hass.services.async_register(
            DOMAIN, "set_charger_current", handle_set_charger_current
        )

    except Exception as err:
        _LOGGER.warning("Exception while setting up Emporia Vue. Will retry. %s", err)
        raise ConfigEntryNotReady(
            f"Exception while setting up Emporia Vue. Will retry. {err}"
        )

    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: vue,
        "coordinator_1min": coordinator_1min,
        "coordinator_1hr": coordinator_1hr,
        "coordinator_day_sensor": coordinator_day_sensor,
    }

    try:
        for component in PLATFORMS:
            hass.async_create_task(
                hass.config_entries.async_forward_entry_setup(entry, component)
            )
    except Exception as err:
        _LOGGER.warning("Error setting up platforms: %s", err)
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
            utcnow = datetime.now(timezone.utc)
            usage_dict = await loop.run_in_executor(
                None, vue.get_device_list_usage, device_gids, utcnow, scale
            )
            if not usage_dict:
                _LOGGER.warning(
                    "No channels found during update for scale %s. Retrying", scale
                )
                usage_dict = await loop.run_in_executor(
                    None, vue.get_device_list_usage, device_gids, utcnow, scale
                )
            if usage_dict:
                recurse_usage_data(usage_dict, scale, data)
            else:
                raise UpdateFailed(f"No channels found during update for scale {scale}")

        return data
    except Exception as err:
        _LOGGER.error("Error communicating with Emporia API: %s", err)
        raise UpdateFailed(f"Error communicating with Emporia API: {err}")


def recurse_usage_data(usage_devices, scale, data):
    for gid, device in usage_devices.items():
        for channel_num, channel in device.channels.items():
            if not channel:
                continue
            reset_datetime = None
            identifier = make_channel_id(channel, scale)
            info = find_device_info_for_channel(channel)
            if scale in [Scale.DAY.value, Scale.MONTH.value]:
                # We need to know when the value reset
                # For day, that should be midnight local time, but we need to use the timestamp returned to us
                # for month, that should be midnight of the reset day they specify in the app

                # in either case, convert the given timestamp to local time first

                local_time = change_time_to_local(device.timestamp, info.time_zone)
                # take the timestamp, convert to midnight
                reset_datetime = local_time.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if scale is Scale.MONTH.value:
                    # Month should use the last billing_cycle_start_day of either this or last month
                    reset_day = info.billing_cycle_start_day
                    if reset_datetime.day >= reset_day:
                        # we're past the reset day for this month, just set the day on the local time
                        reset_datetime = reset_datetime.replace(day=reset_day)
                    else:
                        # we're in the start of a month, roll back to the reset_day of last month
                        reset_datetime = reset_datetime.replace(
                            day=reset_day
                        ) - dateutil.relativedelta.relativedelta(months=1)

                # _LOGGER.info(
                #     "Reset time for %s is %s", identifier, reset_datetime.isoformat()
                # )

            data[identifier] = {
                "device_gid": gid,
                "channel_num": channel_num,
                "usage": fix_usage_sign(channel_num, channel.usage),
                "scale": scale,
                "info": info,
                "reset": reset_datetime,
            }
            if channel.nested_devices:
                recurse_usage_data(channel.nested_devices, scale, data)


def find_device_info_for_channel(channel):
    device_info = None
    if channel.device_gid in device_information:
        device_info = device_information[channel.device_gid]
        if channel.channel_num in [
            "MainsFromGrid",
            "MainsToGrid",
            "Balance",
            "TotalUsage",
        ]:
            found = False
            channel_123 = None
            for device_channel in device_info.channels:
                if device_channel.channel_num == channel.channel_num:
                    found = True
                    break
                if device_channel.channel_num == "1,2,3":
                    channel_123 = device_channel
            if not found:
                _LOGGER.info(
                    "Adding channel for channel %s-%s",
                    channel.device_gid,
                    channel.channel_num,
                )
                device_info.channels.append(
                    VueDeviceChannel(
                        gid=channel.device_gid,
                        name=channel.name,
                        channelNum=channel.channel_num,
                        channelMultiplier=channel_123.channel_multiplier,
                        channelTypeGid=channel_123.channel_type_gid,
                    )
                )
    return device_info


def make_channel_id(channel, scale):
    """Format the channel id for a channel and scale"""
    return "{0}-{1}-{2}".format(channel.device_gid, channel.channel_num, scale)


def fix_usage_sign(channel_num, usage):
    """If the channel is not '1,2,3' or 'Balance' we need it to be positive (see https://github.com/magico13/ha-emporia-vue/issues/57)"""
    if usage and channel_num not in ["1,2,3", "Balance"]:
        return abs(usage)
    if not usage:
        usage = 0
    return usage


def change_time_to_local(time: datetime, tz_string: str):
    """Change the datetime to the provided timezone, if not already."""
    tz_info = dateutil.tz.gettz(tz_string)
    if not time.tzinfo or time.tzinfo.utcoffset(time) is None:
        # unaware, assume it's already utc
        time = time.replace(tzinfo=datetime.timezone.utc)
    return time.astimezone(tz_info)


def handle_midnight(now: datetime, device_gid: int, day_id: str):
    """If midnight has recently passed, reset the last_day_data for Day sensors to zero"""
    global device_information
    global last_day_data
    if device_gid in device_information:
        device_info = device_information[device_gid]
        local_time = change_time_to_local(now, device_info.time_zone)
        local_midnight = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
        if (local_time - local_midnight) < timedelta(minutes=1, seconds=45):
            # Midnight happened since the last update, reset to zero
            _LOGGER.warning(
                "Midnight happened recently for id %s! Current time is %s, midnight is %s",
                day_id,
                local_time,
                local_midnight,
            )
            last_day_data[day_id]["usage"] = 0
            last_day_data[day_id]["reset"] = local_midnight
