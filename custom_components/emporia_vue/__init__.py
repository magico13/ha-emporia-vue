"""The Emporia Vue integration."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any, Optional

import dateutil.relativedelta
import dateutil.tz
from pyemvue import PyEmVue
from pyemvue.device import (
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale
import requests
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, ENABLE_1D, ENABLE_1M, ENABLE_1MON, VUE_DATA

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

DEVICE_GIDS: list[int] = []
DEVICE_INFORMATION: dict[int, VueDevice] = {}
LAST_MINUTE_DATA: dict[str, Any] = {}
LAST_DAY_DATA: dict[str, Any] = {}
LAST_DAY_UPDATE: Optional[datetime] = None

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
    global DEVICE_GIDS
    global DEVICE_INFORMATION
    DEVICE_GIDS = []
    DEVICE_INFORMATION = {}

    entry_data = entry.data
    email = entry_data[CONF_EMAIL]
    password = entry_data[CONF_PASSWORD]
    vue = PyEmVue()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, vue.login, email, password)
        #result = await loop.run_in_executor(None, vue.login_simulator, "http://localhost:8000", email, password)
        if not result:
            raise Exception("Could not authenticate with Emporia API")
    except Exception:
        _LOGGER.error("Could not authenticate with Emporia API")
        return False

    try:
        devices = await loop.run_in_executor(None, vue.get_devices)
        for device in devices:
            if device.device_gid not in DEVICE_GIDS:
                DEVICE_GIDS.append(device.device_gid)
                _LOGGER.info("Adding gid %s to DEVICE_GIDS list", device.device_gid)
                # await loop.run_in_executor(None, vue.populate_device_properties, device)
                DEVICE_INFORMATION[device.device_gid] = device
            else:
                DEVICE_INFORMATION[device.device_gid].channels += device.channels

        total_channels = 0
        for _, device in DEVICE_INFORMATION.items():
            total_channels += len(device.channels)
        _LOGGER.info(
            "Found %s Emporia devices with %s total channels",
            len(DEVICE_INFORMATION.keys()),
            total_channels,
        )

        async def async_update_data_1min():
            """Fetch data from API endpoint at a 1 minute interval.

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            data = await update_sensors(vue, [Scale.MINUTE.value])
            # store this, then have the daily sensors pull from it and integrate
            # then the daily can "true up" hourly (or more frequent) in case it's incorrect
            if data:
                global LAST_MINUTE_DATA
                LAST_MINUTE_DATA = data
            return data

        async def async_update_data_1mon():
            """Fetch data from API endpoint at a 1 hour interval.

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            return await update_sensors(vue, [Scale.MONTH.value])

        async def async_update_day_sensors():
            global LAST_DAY_UPDATE
            global LAST_DAY_DATA
            now = datetime.now(timezone.utc)
            if not LAST_DAY_UPDATE or (now - LAST_DAY_UPDATE) > timedelta(minutes=15):
                _LOGGER.info("Updating day sensors")
                LAST_DAY_UPDATE = now
                LAST_DAY_DATA = await update_sensors(vue, [Scale.DAY.value])
            else:
                # integrate the minute data
                _LOGGER.info("Integrating minute data into day sensors")
                if LAST_MINUTE_DATA:
                    for identifier, data in LAST_MINUTE_DATA.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        day_id = f"{device_gid}-{channel_gid}-{Scale.DAY.value}"
                        if (
                            data
                            and LAST_DAY_DATA
                            and day_id in LAST_DAY_DATA
                            and LAST_DAY_DATA[day_id]
                            and "usage" in LAST_DAY_DATA[day_id]
                            and LAST_DAY_DATA[day_id]["usage"] is not None
                        ):
                            # if we just passed midnight, then reset back to zero
                            timestamp: datetime = data["timestamp"]
                            check_for_midnight(timestamp, int(device_gid), day_id)

                            LAST_DAY_DATA[day_id]["usage"] += data[
                                "usage"
                            ]  # already in kwh
            return LAST_DAY_DATA

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
        coordinator_1mon = None
        if ENABLE_1MON not in entry_data or entry_data[ENABLE_1MON]:
            coordinator_1mon = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="sensor",
                update_method=async_update_data_1mon,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(hours=1),
            )
            await coordinator_1mon.async_config_entry_first_refresh()
            _LOGGER.info("1mon Update data: %s", coordinator_1mon.data)

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
            """Handle setting the EV Charger current."""
            _LOGGER.debug(
                "executing set_charger_current: %s %s",
                str(call.service),
                str(call.data),
            )
            current = call.data.get("current")
            current = int(current)
            device_id = call.data.get("device_id", None)
            entity_id = call.data.get("entity_id", None)

            # if device or entity ids are strings, convert to list
            if isinstance(device_id, str):
                device_id = [device_id]
            if isinstance(entity_id, str):
                entity_id = [entity_id]

            # technically we should loop through all the passed device and entities and update all
            # but for now we'll just use the first one
            charger_entity = None
            entity_registry = er.async_get(hass)
            if device_id:
                entities = er.async_entries_for_device(entity_registry, device_id[0])
                for entity in entities:
                    _LOGGER.info("Entity is %s", str(entity))
                    if entity.entity_id.startswith("switch"):
                        charger_entity = entity
                        break
                if not charger_entity and entities:
                    charger_entity = entities[0]
            elif entity_id:
                charger_entity = entity_registry.async_get(entity_id[0])
            if not charger_entity:
                raise HomeAssistantError("Target device or Entity required.")

            unique_entity_id = charger_entity.unique_id
            gid_match = re.search(r"\d+", unique_entity_id)
            if not gid_match:
                raise HomeAssistantError(
                    f"Could not find device gid from unique id {unique_entity_id}"
                )

            charger_gid = int(gid_match.group(0))
            if (
                charger_gid not in DEVICE_INFORMATION
                or not DEVICE_INFORMATION[charger_gid].ev_charger
            ):
                raise HomeAssistantError(
                    f"Set Charging Current called on invalid device with entity id {charger_entity.entity_id} (unique id {unique_entity_id})"
                )

            state = hass.states.get(charger_entity.entity_id)
            _LOGGER.info("State is %s", str(state))
            if not state:
                raise HomeAssistantError(
                    f"Could not find state for entity {charger_entity.entity_id}"
                )
            charger_info = DEVICE_INFORMATION[charger_gid]
            if charger_info.ev_charger is None:
                raise HomeAssistantError(
                    f"Could not find charger info for device {charger_gid}"
                )
            # Scale the current to a minimum of 6 amps and max of the circuit max
            current = max(6, current)
            current = min(current, charger_info.ev_charger.max_charging_rate)
            _LOGGER.info(
                "Setting charger %s to current of %d amps", charger_gid, current
            )

            try:
                updated_charger = await loop.run_in_executor(
                    None, vue.update_charger, charger_info.ev_charger, state.state == "on", current
                )
                DEVICE_INFORMATION[charger_gid].ev_charger = updated_charger
                # update the state of the charger entity using the updated data
                state = hass.states.get(charger_entity.entity_id)
                if state:
                    newState = "on" if updated_charger.charger_on else "off"
                    newAttributes = state.attributes.copy()
                    newAttributes["charging_rate"] = updated_charger.charging_rate
                    # good enough for now, update the state in the registry
                    hass.states.async_set(charger_entity.entity_id, newState, newAttributes)

            except requests.exceptions.HTTPError as err:
                _LOGGER.error(
                    "Error updating charger status: %s \nResponse body: %s",
                    err,
                    err.response.text,
                )
                raise

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
        "coordinator_1mon": coordinator_1mon,
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


async def update_sensors(vue: PyEmVue, scales: list[str]):
    """Fetch data from API endpoint."""
    try:
        # Note: asyncio.TimeoutError and aiohttp.ClientError are already
        # handled by the data update coordinator.
        data = {}
        loop = asyncio.get_event_loop()
        for scale in scales:
            utcnow = datetime.now(timezone.utc)
            # If we are fetching minute data, there is a strong possibility that
            # we will fetch an incomplete minute that is not cumulative. Therefore,
            # always ensure that we're fetching the previous minute. Unlike DAY and
            # MONTH values, we are not calculating a reset value.
            if scale is Scale.MINUTE.value:
                utcnow = utcnow+timedelta(seconds=-(utcnow.second+1))
            usage_dict = await loop.run_in_executor(
                None, vue.get_device_list_usage, DEVICE_GIDS, utcnow, scale
            )
            if not usage_dict:
                _LOGGER.warning(
                    "No channels found during update for scale %s. Retrying", scale
                )
                usage_dict = await loop.run_in_executor(
                    None, vue.get_device_list_usage, DEVICE_GIDS, utcnow, scale
                )
            if usage_dict:
                flattened, data_time = flatten_usage_data(usage_dict, scale)
                await parse_flattened_usage_data(
                    flattened,
                    scale,
                    data,
                    utcnow,
                    data_time,
                )
            else:
                raise UpdateFailed(f"No channels found during update for scale {scale}")

        return data
    except Exception as err:
        _LOGGER.error("Error communicating with Emporia API: %s", err)
        raise UpdateFailed(f"Error communicating with Emporia API: {err}")


def flatten_usage_data(
    usage_devices: dict[int, VueUsageDevice],
    scale: str,
) -> tuple[dict[str, VueDeviceChannelUsage], datetime]:
    """Flattens the raw usage data into a dictionary of channel ids and usage info."""
    flattened: dict[str, VueDeviceChannelUsage] = {}
    data_time: datetime = datetime.now(timezone.utc)
    for _, usage in usage_devices.items():
        data_time = usage.timestamp or data_time
        if usage.channels:
            for _, channel in usage.channels.items():
                identifier = make_channel_id(channel, scale)
                flattened[identifier] = channel
                if channel.nested_devices:
                    nested_flattened, _ = flatten_usage_data(
                        channel.nested_devices, scale
                    )
                    flattened.update(nested_flattened)
    return (flattened, data_time)


async def parse_flattened_usage_data(
    flattened_data: dict[str, VueDeviceChannelUsage],
    scale: str,
    data: dict[str, Any],
    requested_time: datetime,
    data_time: datetime,
):
    """Loop through the device list and find the corresponding update data."""
    unused_data = flattened_data.copy()
    for gid, info in DEVICE_INFORMATION.items():
        local_time = change_time_to_local(data_time, info.time_zone)
        requested_time_local = change_time_to_local(requested_time, info.time_zone)
        if abs((local_time - requested_time_local).total_seconds()) > 30:
            _LOGGER.warning(
                "More than 30 seconds have passed between the requested datetime and the returned datetime. Requested: %s Returned: %s",
                requested_time,
                data_time,
            )
        for info_channel in info.channels:
            identifier = make_channel_id(info_channel, scale)
            channel_num = info_channel.channel_num
            channel = (
                flattened_data[identifier] if identifier in flattened_data else None
            )
            if not channel:
                _LOGGER.info(
                    "Could not find usage info for device %s channel %s",
                    gid,
                    channel_num,
                )
            unused_data.pop(identifier, None)
            reset_datetime = None

            if scale in [Scale.DAY.value, Scale.MONTH.value]:
                # We need to know when the value reset
                # For day, that should be midnight local time, but we need to use the timestamp returned to us
                # for month, that should be midnight of the reset day they specify in the app
                reset_datetime = determine_reset_datetime(
                    local_time,
                    info.billing_cycle_start_day,
                    scale == Scale.MONTH.value,
                )

            # Fix the usage if we got None
            # Use the last value if we have it, otherwise use zero
            fixed_usage = channel.usage if channel else 0
            if fixed_usage is None:
                fixed_usage = handle_none_usage(scale, identifier)
                _LOGGER.info(
                    "Got None usage for device %s channel %s scale %s and timestamp %s. Instead using a value of %s",
                    gid,
                    channel_num,
                    scale,
                    local_time.isoformat(),
                    fixed_usage,
                )

            fixed_usage = fix_usage_sign(channel_num, fixed_usage)

            data[identifier] = {
                "device_gid": gid,
                "channel_num": channel_num,
                "usage": fixed_usage,
                "scale": scale,
                "info": info,
                "reset": reset_datetime,
                "timestamp": local_time,
            }
    if unused_data:
        # unused_data is not json serializable because VueDeviceChannelUsage is not JSON serializable
        # instead print out dictionary as a string
        _LOGGER.warning(
            "Unused data found during update. Unused data: %s",
            str(unused_data),
        )
        channels_were_added = False
        for _, channel in unused_data.items():
            channels_were_added |= await handle_special_channels_for_device(channel)
            # we'll also need to register these entities I think. They might show up automatically on the first run
        # When we're done handling the unused data we need to rerun the update
        if channels_were_added:
            _LOGGER.info("Rerunning update due to added channels")
            await parse_flattened_usage_data(flattened_data, scale, data, requested_time, data_time)


async def handle_special_channels_for_device(channel: VueDeviceChannel) -> bool:
    """Handle the special channels for a device, if they exist."""
    device_info = None
    if channel.device_gid in DEVICE_INFORMATION:
        device_info = DEVICE_INFORMATION[channel.device_gid]
        # if channel.channel_num in [
        #     "MainsFromGrid",
        #     "MainsToGrid",
        #     "Balance",
        #     "TotalUsage",
        # ]:
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
            multiplier = 1.0
            type_gid = 1
            if channel_123:
                multiplier = channel_123.channel_multiplier
                type_gid = channel_123.channel_type_gid

            device_info.channels.append(
                VueDeviceChannel(
                    gid=channel.device_gid,
                    name=channel.name,
                    channelNum=channel.channel_num,
                    channelMultiplier=multiplier,
                    channelTypeGid=type_gid,
                )
            )

                # register the entity
                # registry = await async_get_registry(hass)
                # registry.async_get_or_create(
                #     domain='your_domain',
                #     platform='your_platform',
                #     unique_id=entity_id,
                #     name=entity_name,
                #     config_entry=config_entry,
                #     device_id=device_id,
                # )
            return True
    return False


def make_channel_id(channel: VueDeviceChannel, scale: str):
    """Format the channel id for a channel and scale."""
    return f"{channel.device_gid}-{channel.channel_num}-{scale}"


def fix_usage_sign(channel_num: str, usage: float):
    """If the channel is not '1,2,3' or 'Balance' we need it to be positive (see https://github.com/magico13/ha-emporia-vue/issues/57)."""
    if usage and channel_num not in ["1,2,3", "Balance"]:
        return abs(usage)
    return usage


def change_time_to_local(time: datetime, tz_string: str):
    """Change the datetime to the provided timezone, if not already."""
    tz_info = dateutil.tz.gettz(tz_string)
    if not time.tzinfo or time.tzinfo.utcoffset(time) is None:
        # unaware, assume it's already utc
        time = time.replace(tzinfo=timezone.utc)
    return time.astimezone(tz_info)


def check_for_midnight(timestamp: datetime, device_gid: int, day_id: str):
    """If midnight has recently passed, reset the LAST_DAY_DATA for Day sensors to zero."""
    if device_gid in DEVICE_INFORMATION:
        device_info = DEVICE_INFORMATION[device_gid]
        local_time = change_time_to_local(timestamp, device_info.time_zone)
        local_midnight = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
        last_reset = LAST_DAY_DATA[day_id]["reset"]
        if local_midnight > last_reset:
            # New reset time found
            _LOGGER.info(
                "Midnight happened recently for id %s! Timestamp is %s, midnight is %s, previous reset was %s",
                day_id,
                local_time,
                local_midnight,
                last_reset,
            )
            LAST_DAY_DATA[day_id]["usage"] = 0
            LAST_DAY_DATA[day_id]["reset"] = local_midnight


def determine_reset_datetime(
    local_time: datetime, monthly_cycle_start: int, is_month: bool
):
    """Determine the last reset datetime (aware) based on the passed time and cycle start date."""
    reset_datetime = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
    if is_month:
        # Month should use the last billing_cycle_start_day of either this or last month
        reset_datetime = reset_datetime.replace(day=monthly_cycle_start)
        if reset_datetime.day < monthly_cycle_start:
            # we're in the start of a month, use the reset_day for last month
            reset_datetime -= dateutil.relativedelta.relativedelta(months=1)
    return reset_datetime


def handle_none_usage(scale: str, identifier: str):
    """Handle the case of the usage being None by using the previous value or zero."""
    if (
        scale is Scale.MINUTE.value
        and identifier in LAST_MINUTE_DATA
        and "usage" in LAST_MINUTE_DATA[identifier]
    ):
        return LAST_MINUTE_DATA[identifier]["usage"]
    if (
        scale is Scale.DAY.value
        and identifier in LAST_DAY_DATA
        and "usage" in LAST_DAY_DATA[identifier]
    ):
        return LAST_DAY_DATA[identifier]["usage"]
    return 0
