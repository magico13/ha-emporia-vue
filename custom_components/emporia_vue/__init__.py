"""The Emporia Vue integration."""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from functools import partial
import logging
import re
from typing import Any, TypeAlias

import dateutil.relativedelta
import dateutil.tz
from pyemvue import PyEmVue
from pyemvue.device import (
    ChargerDevice,
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
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    AUTH_METHOD,
    AUTH_METHOD_EMAIL_PASSWORD,
    AUTH_METHOD_TOKENS,
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONFIG_FLOW_SCHEMA,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "switch", "number"]
SENSITIVE_CONFIG_KEYS = {
    CONF_PASSWORD,
    CONF_ID_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
}

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: CONFIG_FLOW_SCHEMA},
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class EmporiaVueData:
    """Mutable runtime state for a single Emporia Vue config entry.

    This is kept on the config entry itself (`entry.runtime_data`) rather
    than as module-level globals so that entry reloads and (future)
    multiple accounts can't clobber each other's device/usage caches.
    """

    vue: PyEmVue
    invert_solar: bool = True
    device_gids: list[str] = field(default_factory=list)
    device_information: dict[int, VueDevice] = field(default_factory=dict)
    last_minute_data: dict[str, Any] = field(default_factory=dict)
    last_day_data: dict[str, Any] = field(default_factory=dict)
    last_day_update: datetime | None = None
    last_month_data: dict[str, Any] = field(default_factory=dict)
    last_month_update: datetime | None = None
    coordinator_1min: DataUpdateCoordinator | None = None
    coordinator_1mon: DataUpdateCoordinator | None = None
    coordinator_day_sensor: DataUpdateCoordinator | None = None
    coordinator_device_status: DataUpdateCoordinator | None = None


EmporiaVueConfigEntry: TypeAlias = ConfigEntry[EmporiaVueData]


def redact_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data with sensitive auth values hidden for logging."""
    return {
        key: "***" if key in SENSITIVE_CONFIG_KEYS else value
        for key, value in data.items()
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Emporia Vue component."""
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
                SOLAR_INVERT: conf[SOLAR_INVERT],
            },
        )
    )
    return True


async def async_login_vue(
    hass: HomeAssistant,
    vue: PyEmVue,
    entry_data: Mapping[str, Any],
) -> bool:
    """Log in to Emporia using the configured authentication method."""
    auth_method = entry_data.get(AUTH_METHOD, AUTH_METHOD_EMAIL_PASSWORD)
    if auth_method == AUTH_METHOD_TOKENS:
        return await hass.async_add_executor_job(
            partial(
                vue.login,
                id_token=entry_data[CONF_ID_TOKEN],
                access_token=entry_data[CONF_ACCESS_TOKEN],
                refresh_token=entry_data[CONF_REFRESH_TOKEN],
            ),
        )

    email: str = entry_data[CONF_EMAIL]
    password: str = entry_data[CONF_PASSWORD]
    # support using the simulator by looking at the username
    if email.startswith("vue_simulator@"):
        host = email.split("@")[1]
        return await hass.async_add_executor_job(vue.login_simulator, host)
    return await hass.async_add_executor_job(
        partial(vue.login, username=email, password=password),
    )


async def async_setup_entry(hass: HomeAssistant, entry: EmporiaVueConfigEntry) -> bool:
    """Set up Emporia Vue from a config entry."""
    entry_data = entry.data
    _LOGGER.debug(
        "Setting up Emporia Vue with entry data: %s",
        redact_config_data(entry_data),
    )
    # Import lazily so a failure in the compatibility layer surfaces as a
    # normal setup error rather than an import-time failure of this module.
    from .api_v1 import apply_v1_compatibility

    vue = apply_v1_compatibility(PyEmVue())
    data = EmporiaVueData(
        vue=vue,
        invert_solar=entry_data.get(SOLAR_INVERT, True),
    )

    try:
        result: bool = await async_login_vue(hass, vue, entry_data)
        if not result:
            raise ConfigEntryAuthFailed("Failed to login to Emporia Vue")
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # pylint: disable=broad-exception-caught
        _LOGGER.error("Failed to login to Emporia Vue: %s", err)
        raise ConfigEntryAuthFailed("Failed to login to Emporia Vue") from err

    if entry_data.get(AUTH_METHOD) == AUTH_METHOD_TOKENS and vue.auth and vue.auth.tokens:
        # Persist the tokens refreshed during login back to the config entry so
        # that the stored tokens stay current across restarts.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ID_TOKEN: vue.auth.tokens["id_token"],
                CONF_ACCESS_TOKEN: vue.auth.tokens["access_token"],
                CONF_REFRESH_TOKEN: vue.auth.tokens["refresh_token"],
            },
        )

        def _token_updater(tokens: dict[str, Any]) -> None:
            """Persist tokens refreshed mid-session back to the config entry."""
            hass.loop.call_soon_threadsafe(
                lambda: hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_ID_TOKEN: tokens["id_token"],
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    },
                )
            )

        vue.auth.token_updater = _token_updater

    try:
        devices: list[VueDevice] = await hass.async_add_executor_job(vue.get_devices)
    except (requests.exceptions.RequestException, OSError) as err:
        raise ConfigEntryNotReady(f"Error fetching Emporia Vue devices: {err}") from err

    for device in devices:
        if str(device.device_gid) not in data.device_gids:
            data.device_gids.append(str(device.device_gid))
            _LOGGER.info("Adding gid %s to device list", device.device_gid)
            data.device_information[device.device_gid] = device
        else:
            data.device_information[device.device_gid].channels += device.channels

    total_channels = sum(len(device.channels) for device in data.device_information.values())
    _LOGGER.info(
        "Found %s Emporia devices with %s total channels",
        len(data.device_information),
        total_channels,
    )

    async def async_update_data_1min() -> dict:
        """Fetch data from API endpoint at a 1 minute interval.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        result_data: dict = await update_sensors(hass, vue, [Scale.MINUTE.value], data)
        # store this, then have the daily sensors pull from it and integrate
        # then the daily can "true up" hourly (or more frequent) in case it's incorrect
        if result_data:
            data.last_minute_data = result_data
        return result_data

    async def async_update_day_sensors() -> dict:
        now: datetime = datetime.now(UTC)
        if not data.last_day_update or (now - data.last_day_update) > timedelta(minutes=15):
            _LOGGER.info("Updating day sensors")
            data.last_day_update = now
            updated_day_data = await update_sensors(hass, vue, [Scale.DAY.value], data)
            apply_api_update_debounce(updated_day_data, data.last_day_data, "day")
            data.last_day_data = updated_day_data
        else:
            # integrate the minute data
            _LOGGER.info("Integrating minute data into day sensors")
            if data.last_minute_data:
                for identifier, minute_data in data.last_minute_data.items():
                    device_gid, channel_gid, _ = identifier.split("-")
                    day_id: str = f"{device_gid}-{channel_gid}-{Scale.DAY.value}"
                    if (
                        minute_data
                        and data.last_day_data
                        and day_id in data.last_day_data
                        and data.last_day_data[day_id]
                        and "usage" in data.last_day_data[day_id]
                        and data.last_day_data[day_id]["usage"] is not None
                    ):
                        # if we just passed midnight, then reset back to zero
                        timestamp: datetime = minute_data["timestamp"]
                        await check_for_midnight(hass, timestamp, int(device_gid), day_id, data)

                        data.last_day_data[day_id]["usage"] += minute_data[
                            "usage"
                        ]  # already in kwh
        return data.last_day_data

    async def async_update_month_sensors() -> dict:
        now: datetime = datetime.now(UTC)
        if not data.last_month_update or (now - data.last_month_update) > timedelta(
            minutes=30
        ):
            _LOGGER.info("Updating month sensors")
            data.last_month_update = now
            updated_month_data = await update_sensors(hass, vue, [Scale.MONTH.value], data)
            apply_api_update_debounce(
                updated_month_data,
                data.last_month_data,
                "month",
            )
            data.last_month_data = updated_month_data
        else:
            # integrate the minute data
            _LOGGER.info("Integrating minute data into month sensors")
            if data.last_minute_data:
                for identifier, minute_data in data.last_minute_data.items():
                    device_gid, channel_gid, _ = identifier.split("-")
                    month_id: str = f"{device_gid}-{channel_gid}-{Scale.MONTH.value}"
                    if (
                        minute_data
                        and data.last_month_data
                        and month_id in data.last_month_data
                        and data.last_month_data[month_id]
                        and "usage" in data.last_month_data[month_id]
                        and data.last_month_data[month_id]["usage"] is not None
                    ):
                        # if we just passed the billing cycle start, reset back to zero
                        timestamp: datetime = minute_data["timestamp"]
                        await check_for_new_month(
                            hass, timestamp, int(device_gid), month_id, data
                        )

                        data.last_month_data[month_id]["usage"] += minute_data[
                            "usage"
                        ]  # already in kwh
        return data.last_month_data

    if ENABLE_1M not in entry_data or entry_data[ENABLE_1M]:
        data.coordinator_1min = DataUpdateCoordinator(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="sensor",
            update_method=async_update_data_1min,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=1),
        )
        await data.coordinator_1min.async_config_entry_first_refresh()
        _LOGGER.debug("1min Update data: %s", data.coordinator_1min.data)

    if ENABLE_1MON not in entry_data or entry_data[ENABLE_1MON]:
        data.coordinator_1mon = DataUpdateCoordinator(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="sensor",
            update_method=async_update_month_sensors,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=1),
        )
        await data.coordinator_1mon.async_config_entry_first_refresh()
        _LOGGER.debug("1mon Update data: %s", data.coordinator_1mon.data)

    if ENABLE_1D not in entry_data or entry_data[ENABLE_1D]:
        data.coordinator_day_sensor = DataUpdateCoordinator(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="sensor",
            update_method=async_update_day_sensors,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=1),
        )
        await data.coordinator_day_sensor.async_config_entry_first_refresh()

    # Check if any devices have outlets or chargers
    has_controllable_devices = any(
        device.outlet or device.ev_charger for device in data.device_information.values()
    )

    async def async_update_device_status() -> dict[str, Any]:
        """Fetch device status (outlets and chargers)."""
        try:
            status_data: dict[str, Any] = {}
            outlets, chargers = await hass.async_add_executor_job(vue.get_devices_status)

            if outlets:
                for outlet in outlets:
                    status_data[str(outlet.device_gid)] = outlet
            if chargers:
                for charger in chargers:
                    status_data[str(charger.device_gid)] = charger
            return status_data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err

    if has_controllable_devices:
        data.coordinator_device_status = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="device_status",
            update_method=async_update_device_status,
            update_interval=timedelta(minutes=1),
        )
        await data.coordinator_device_status.async_config_entry_first_refresh()

    # Setup custom services
    async def handle_set_charger_current(call) -> None:
        """Handle setting the EV Charger current."""
        _LOGGER.debug(
            "executing set_charger_current: %s %s",
            str(call.service),
            str(call.data),
        )
        current = int(call.data.get("current"))
        device_id: str | list[str] | None = call.data.get("device_id", None)
        entity_id: str | list[str] | None = call.data.get("entity_id", None)

        # if device or entity ids are strings, convert to list
        if isinstance(device_id, str):
            device_id = [device_id]
        if isinstance(entity_id, str):
            entity_id = [entity_id]

        # technically we should loop through all the passed device and entities and update all
        # but for now we'll just use the first one
        charger_entity: er.RegistryEntry | None = None
        entity_registry: er.EntityRegistry = er.async_get(hass)
        if device_id:
            entities: list[er.RegistryEntry] = er.async_entries_for_device(
                entity_registry, device_id[0]
            )
            for reg_entity in entities:
                _LOGGER.info("Entity is %s", str(reg_entity))
                if reg_entity.entity_id.startswith("switch"):
                    charger_entity = reg_entity
                    break
            if not charger_entity and entities:
                charger_entity = entities[0]
        elif entity_id:
            charger_entity = entity_registry.async_get(entity_id[0])
        if not charger_entity:
            raise HomeAssistantError("Target device or Entity required.")

        unique_entity_id: str = charger_entity.unique_id
        gid_match: re.Match[str] | None = re.search(r"\d+", unique_entity_id)
        if not gid_match:
            raise HomeAssistantError(
                f"Could not find device gid from unique id {unique_entity_id}"
            )

        charger_gid = int(gid_match.group(0))
        if (
            charger_gid not in data.device_information
            or not data.device_information[charger_gid].ev_charger
        ):
            raise HomeAssistantError(
                "Set Charging Current called on invalid device with entity id"
                f" {charger_entity.entity_id} (unique id {unique_entity_id})"
            )

        state = hass.states.get(charger_entity.entity_id)
        _LOGGER.info("State is %s", str(state))
        if not state:
            raise HomeAssistantError(
                f"Could not find state for entity {charger_entity.entity_id}"
            )
        charger_info: VueDevice = data.device_information[charger_gid]
        if charger_info.ev_charger is None:
            raise HomeAssistantError(f"Could not find charger info for device {charger_gid}")
        # Scale the current to a minimum of 6 amps and max of the circuit max
        current = max(6, current)
        current = min(current, charger_info.ev_charger.max_charging_rate)
        _LOGGER.info("Setting charger %s to current of %d amps", charger_gid, current)

        try:
            updated_charger: ChargerDevice = await hass.async_add_executor_job(
                vue.update_charger,
                charger_info.ev_charger,
                state.state == "on",
                current,
            )
            data.device_information[charger_gid].ev_charger = updated_charger
            # update the state of the charger entity using the updated data
            new_state_obj: State | None = hass.states.get(charger_entity.entity_id)
            if new_state_obj:
                new_state: str = "on" if updated_charger.charger_on else "off"
                new_attributes: dict = new_state_obj.attributes.copy()
                new_attributes["charging_rate"] = updated_charger.charging_rate
                # good enough for now, update the state in the registry
                hass.states.async_set(charger_entity.entity_id, new_state, new_attributes)

        except requests.exceptions.HTTPError as err:
            _LOGGER.error(
                "Error updating charger status: %s \nResponse body: %s",
                err,
                err.response.text,
            )
            raise

    # Guard against double-registration on entry reload, and unregister the
    # service when this (the only allowed) entry is unloaded.
    if not hass.services.has_service(DOMAIN, "set_charger_current"):
        hass.services.async_register(DOMAIN, "set_charger_current", handle_set_charger_current)
        entry.async_on_unload(
            lambda: hass.services.async_remove(DOMAIN, "set_charger_current")
        )

    entry.runtime_data = data

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as err:
        _LOGGER.warning("Error setting up platforms: %s", err)
        raise ConfigEntryNotReady(f"Error setting up platforms: {err}") from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmporiaVueConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_sensors(
    hass: HomeAssistant,
    vue: PyEmVue,
    scales: list[str],
    data: EmporiaVueData,
) -> dict:
    """Fetch data from API endpoint.

    Retries/backoff for transient API errors already live in
    `api_v1.apply_v1_compatibility`'s `get_device_list_usage` patch, so this
    function does not layer a second retry on top of that one.
    """
    try:
        result_data: dict = {}
        for scale in scales:
            utcnow: datetime = datetime.now(UTC)
            usage_dict: dict[int, VueUsageDevice] = await hass.async_add_executor_job(
                vue.get_device_list_usage, data.device_gids, utcnow, scale
            )
            if not usage_dict:
                raise UpdateFailed(f"No channels found during update for scale {scale}")
            flattened, data_time = flatten_usage_data(usage_dict, scale)
            await parse_flattened_usage_data(
                hass,
                flattened,
                scale,
                result_data,
                utcnow,
                data_time,
                data,
            )

        return result_data
    except UpdateFailed:
        raise
    except Exception as err:
        _LOGGER.error("Error communicating with Emporia API: %s", err)
        raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err


def flatten_usage_data(
    usage_devices: dict[int, VueUsageDevice],
    scale: str,
) -> tuple[dict[str, VueDeviceChannelUsage], datetime]:
    """Flattens the raw usage data into a dictionary of channel ids and usage info."""
    flattened: dict[str, VueDeviceChannelUsage] = {}
    data_time: datetime = datetime.now(UTC)
    for usage in usage_devices.values():
        data_time = usage.timestamp or data_time
        if usage.channels:
            for channel in usage.channels.values():
                identifier: str = make_channel_id(channel, scale)
                flattened[identifier] = channel
                if channel.nested_devices:
                    nested_flattened, _ = flatten_usage_data(channel.nested_devices, scale)
                    flattened.update(nested_flattened)
    return (flattened, data_time)


async def parse_flattened_usage_data(
    hass: HomeAssistant,
    flattened_data: dict[str, VueDeviceChannelUsage],
    scale: str,
    result_data: dict[str, Any],
    requested_time: datetime,
    data_time: datetime,
    data: EmporiaVueData,
) -> None:
    """Loop through the device list and find the corresponding update data."""
    unused_data: dict[str, VueDeviceChannelUsage] = flattened_data.copy()
    for gid, info in data.device_information.items():
        local_time: datetime = await change_time_to_local(hass, data_time, info.time_zone)
        requested_time_local: datetime = await change_time_to_local(
            hass, requested_time, info.time_zone
        )
        if abs((local_time - requested_time_local).total_seconds()) > 30:
            _LOGGER.warning(
                "More than 30 seconds have passed between the requested datetime"
                " and the returned datetime. Requested: %s Returned: %s",
                requested_time,
                data_time,
            )
        for info_channel in info.channels:
            identifier: str = make_channel_id(info_channel, scale)
            channel_num = info_channel.channel_num
            channel: VueDeviceChannelUsage | None = flattened_data.get(identifier)
            if not channel:
                _LOGGER.info(
                    "Could not find usage info for device %s channel %s",
                    gid,
                    channel_num,
                )
            unused_data.pop(identifier, None)
            reset_datetime: datetime | None = None

            if scale in [Scale.DAY.value, Scale.MONTH.value]:
                # We need to know when the value reset
                # For day, that should be midnight local time, but we need to use the timestamp
                # returnedto us for month, that should be midnight of the reset day they specify
                # in the app
                reset_datetime = determine_reset_datetime(
                    local_time,
                    info.billing_cycle_start_day,
                    scale == Scale.MONTH.value,
                )

            # Fix the usage if we got None
            # Use the last value if we have it, otherwise use zero
            fixed_usage: float = channel.usage if channel else 0.0
            if fixed_usage is None:
                fixed_usage = handle_none_usage(scale, identifier, data)
                _LOGGER.info(
                    "Got None usage for device %s channel %s scale %s and timestamp %s. "
                    "Instead using a value of %s",
                    gid,
                    channel_num,
                    scale,
                    local_time.isoformat(),
                    fixed_usage,
                )

            bidirectional = "bidirectional" in info_channel.type.lower()
            is_solar = info_channel.channel_type_gid == 13
            fixed_usage = fix_usage_sign(
                channel_num, fixed_usage, bidirectional, is_solar, data.invert_solar
            )

            result_data[identifier] = {
                "device_gid": gid,
                "channel_num": channel_num,
                "usage": fixed_usage,
                "scale": scale,
                "info": info,
                "reset": reset_datetime,
                "timestamp": local_time,
            }
    if unused_data:
        # unused_data is not json serializable because VueDeviceChannelUsage
        # is not JSON serializable instead print out dictionary as a string
        _LOGGER.info(
            "Unused data found during update. Unused data: %s",
            str(unused_data),
        )
        channels_were_added = False
        for channel in unused_data.values():
            channels_were_added |= await handle_special_channels_for_device(channel, data)
            # we'll also need to register these entities I think. They might show up
            # automatically on the first run When we're done handling the unused data
            # we need to rerun the update
        if channels_were_added:
            _LOGGER.info("Rerunning update due to added channels")
            await parse_flattened_usage_data(
                hass, flattened_data, scale, result_data, requested_time, data_time, data
            )


async def handle_special_channels_for_device(
    channel: VueDeviceChannel, data: EmporiaVueData
) -> bool:
    """Handle the special channels for a device, if they exist."""
    if channel.device_gid in data.device_information:
        device_info: VueDevice = data.device_information[channel.device_gid]
        found = False
        channel_123: VueDeviceChannel | None = None
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

            return True
    return False


def make_channel_id(channel: VueDeviceChannel, scale: str) -> str:
    """Format the channel id for a channel and scale."""
    return f"{channel.device_gid}-{channel.channel_num}-{scale}"


def fix_usage_sign(
    channel_num: str,
    usage: float,
    bidirectional: bool,
    is_solar: bool,
    invert_solar: bool,
) -> float:
    """If the channel is not '1,2,3' or 'Balance' we need it to be positive.

    Solar circuits are up to the user to decide. Positive is recommended for the energy dashboard.

    (see https://github.com/magico13/ha-emporia-vue/issues/57)
    """
    if is_solar:
        # Energy dashboard wants solar to be positive, Emporia usually provides negative
        if usage and invert_solar:
            return -1 * usage
        return usage

    if usage and not bidirectional and channel_num not in ["1,2,3", "Balance"]:
        # With bidirectionality, we need to also check if bidirectional. If yes,
        # we either don't abs, or we flip the sign.
        return abs(usage)
    return usage


async def change_time_to_local(hass: HomeAssistant, time: datetime, tz_string: str) -> datetime:
    """Change the datetime to the provided timezone, if not already."""
    tz_info: tzinfo | None = await hass.async_add_executor_job(dateutil.tz.gettz, tz_string)
    if not time.tzinfo or time.tzinfo.utcoffset(time) is None:
        # unaware, assume it's already utc
        time = time.replace(tzinfo=UTC)
    return time.astimezone(tz_info)


async def check_for_midnight(
    hass: HomeAssistant,
    timestamp: datetime,
    device_gid: int,
    day_id: str,
    data: EmporiaVueData,
) -> None:
    """If midnight has recently passed, reset the day data for Day sensors to zero."""
    if device_gid in data.device_information:
        device_info: VueDevice = data.device_information[device_gid]
        local_time: datetime = await change_time_to_local(hass, timestamp, device_info.time_zone)
        local_midnight: datetime = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
        last_reset = data.last_day_data[day_id]["reset"]
        if local_midnight > last_reset:
            # New reset time found
            _LOGGER.info(
                "Midnight happened recently for id %s! Timestamp is %s, midnight is %s, "
                "previous reset was %s",
                day_id,
                local_time,
                local_midnight,
                last_reset,
            )
            data.last_day_data[day_id]["usage"] = 0
            data.last_day_data[day_id]["reset"] = local_midnight


async def check_for_new_month(
    hass: HomeAssistant,
    timestamp: datetime,
    device_gid: int,
    month_id: str,
    data: EmporiaVueData,
) -> None:
    """If a new billing cycle has started, reset the month data for Month sensors to zero."""
    if device_gid in data.device_information:
        device_info: VueDevice = data.device_information[device_gid]
        local_time: datetime = await change_time_to_local(hass, timestamp, device_info.time_zone)
        current_reset: datetime = determine_reset_datetime(
            local_time,
            device_info.billing_cycle_start_day,
            True,
        )
        last_reset = data.last_month_data[month_id]["reset"]
        if current_reset > last_reset:
            # New billing cycle started
            _LOGGER.info(
                "New billing cycle started for id %s! Timestamp is %s, "
                "current reset is %s, previous reset was %s",
                month_id,
                local_time,
                current_reset,
                last_reset,
            )
            data.last_month_data[month_id]["usage"] = 0
            data.last_month_data[month_id]["reset"] = current_reset


def determine_reset_datetime(
    local_time: datetime, monthly_cycle_start: int, is_month: bool
) -> datetime:
    """Determine the last reset datetime (aware) based on the passed time and cycle start date."""
    reset_datetime: datetime = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
    if is_month:
        # Month should use the most recent billing_cycle_start_day midnight.
        # Never return a future reset datetime.
        last_day_this_month = calendar.monthrange(reset_datetime.year, reset_datetime.month)[1]
        target_day_this_month = min(monthly_cycle_start, last_day_this_month)
        candidate_this_month = reset_datetime.replace(day=target_day_this_month)

        if local_time >= candidate_this_month:
            reset_datetime = candidate_this_month
        else:
            previous_month = reset_datetime - dateutil.relativedelta.relativedelta(months=1)
            last_day_previous_month = calendar.monthrange(
                previous_month.year, previous_month.month
            )[1]
            target_day_previous_month = min(monthly_cycle_start, last_day_previous_month)
            reset_datetime = previous_month.replace(day=target_day_previous_month)
    return reset_datetime


def handle_none_usage(scale: str, identifier: str, data: EmporiaVueData):
    """Handle the case of the usage being None by using the previous value or zero."""
    if (
        scale is Scale.MINUTE.value
        and identifier in data.last_minute_data
        and "usage" in data.last_minute_data[identifier]
    ):
        return data.last_minute_data[identifier]["usage"]
    if (
        scale is Scale.DAY.value
        and identifier in data.last_day_data
        and "usage" in data.last_day_data[identifier]
    ):
        return data.last_day_data[identifier]["usage"]
    return 0


def apply_api_update_debounce(
    updated_data: dict[str, Any],
    existing_data: dict[str, Any],
    scale_name: str,
) -> None:
    """Prevent API reset lag from inflating totals shortly after local reset time.

    During the debounce window after reset, API values may lag and still include prior
    period usage. In that case, allow API values to lower totals but not raise them
    above the minute-integrated value already tracked in memory.
    """
    if not updated_data or not existing_data:
        return

    for identifier, updated in updated_data.items():
        if identifier not in existing_data or not updated:
            continue

        existing = existing_data[identifier]
        if not existing:
            continue

        updated_usage = updated.get("usage")
        existing_usage = existing.get("usage")
        reset_datetime = updated.get("reset")
        timestamp = updated.get("timestamp")

        if (
            updated_usage is None
            or existing_usage is None
            or reset_datetime is None
            or timestamp is None
        ):
            continue

        if is_in_reset_debounce_window(
            timestamp,
            reset_datetime,
            scale_name,
        ):
            bounded_usage = min(updated_usage, existing_usage)
            if bounded_usage != updated_usage:
                _LOGGER.info(
                    "Debouncing %s API reset lag for %s: keeping %.6f instead of %.6f",
                    scale_name,
                    identifier,
                    bounded_usage,
                    updated_usage,
                )
                updated["usage"] = bounded_usage


def is_in_reset_debounce_window(
    local_time: datetime,
    reset_datetime: datetime,
    scale_name: str,
    debounce_minutes: int = 30,
) -> bool:
    """Return true when local_time is in the reset debounce window for the scale."""
    if scale_name == "month" and local_time.date() != reset_datetime.date():
        # Monthly debounce only applies on billing-cycle reset date rollover.
        return False

    elapsed = local_time - reset_datetime
    return timedelta(0) <= elapsed < timedelta(minutes=debounce_minutes)
