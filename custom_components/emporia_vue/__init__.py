"""The Emporia Vue integration."""

import asyncio
import calendar
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from functools import partial
from typing import Any

import dateutil.relativedelta
import dateutil.tz
import requests
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyemvue import PyEmVue
from pyemvue.device import (
    ChargerDevice,
    OutletDevice,
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale, Unit

from .cognito_compat import apply_cognito_compat
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
    VUE_DATA,
)
from .resilience import TolerantUpdateMethod, is_newer_sample

apply_cognito_compat()

_LOGGER: logging.Logger = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "switch", "number"]
SPLIT_PHASE_MAINS_CHANNELS = ("Mains_A", "Mains_B", "Mains_C")
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

DEVICE_GIDS: list[str] = []
DEVICE_INFORMATION: dict[int, VueDevice] = {}
DEVICES_ONLINE: list[str] = []
LAST_MINUTE_DATA: dict[str, Any] = {}
LAST_DAY_DATA: dict[str, Any] = {}
LAST_DAY_UPDATE: datetime | None = None
LAST_DAY_INTEGRATED_MINUTE: dict[str, datetime] = {}
LAST_MONTH_DATA: dict[str, Any] = {}
LAST_MONTH_UPDATE: datetime | None = None
LAST_MONTH_INTEGRATED_MINUTE: dict[str, datetime] = {}
INVERT_SOLAR: bool = True

TOLERATED_UPDATE_FAILURES = 2


def redact_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data with sensitive auth values hidden for logging."""
    return {
        key: "***" if key in SENSITIVE_CONFIG_KEYS else value
        for key, value in data.items()
    }


SYNTHETIC_VIRTUAL_CIRCUITS: dict[str, str] = {
    "Balance": "Virtual circuit (Balance)",
    "TotalUsage": "Virtual circuit (Total usage)",
    "MainsFromGrid": "Virtual circuit (Mains from grid)",
    "MainsToGrid": "Virtual circuit (Mains to grid)",
}


def determine_circuit_model(
    channel: VueDeviceChannel, device: VueDevice | None = None
) -> str:
    """Return a descriptive circuit model for a physical CT or virtual channel."""
    num = str(channel.channel_num or "")
    if num in SYNTHETIC_VIRTUAL_CIRCUITS:
        return SYNTHETIC_VIRTUAL_CIRCUITS[num]

    # Merged Virtual Circuit constituent branch discovery (virtual channels e.g. Merged_100)
    if num.startswith("Merged_"):
        if device and device.channels:
            member_branches = [
                ch.channel_num
                for ch in device.channels
                if (
                    ch.parent_channel_id in (num, channel.channel_id)
                    or ch.parent_channel_num in (num, channel.channel_id)
                )
                and ch.channel_num.isdigit()
            ]
            if member_branches:
                sorted_branches = sorted(member_branches, key=int)
                return f"Merged virtual circuit (Branches {', '.join(sorted_branches)})"
        return "Merged virtual circuit"

    raw_class = (
        getattr(channel, "channel_classification", None)
        or getattr(channel, "type", None)
        or ""
    )
    classification = str(raw_class).strip().lower()

    if "main" in classification or num in ("1,2,3", "Mains_A", "Mains_B", "Mains_C"):
        return "Main CT circuit"
    if (
        "200" in classification
        or "two_hundred" in classification
        or "twohundred" in classification
    ):
        return "200A CT circuit"
    if "merged" in classification:
        return (
            f"Merged CT circuit (Branch {num})" if num.isdigit() else "Merged CT circuit"
        )

    if num.isdigit():
        return f"50A CT circuit (Branch {num})"

    return "50A CT circuit"


def get_device_depth(d: VueDevice, device_info_map: dict[int, VueDevice]) -> int:
    """Compute nesting depth of a device so parent devices are registered first."""
    depth = 0
    curr = d
    visited = {curr.device_gid}
    parent_gid = curr.parent_device_gid
    while parent_gid and parent_gid != 0 and parent_gid in device_info_map:
        if parent_gid in visited:
            break
        visited.add(parent_gid)
        depth += 1
        curr = device_info_map[parent_gid]
        parent_gid = curr.parent_device_gid
    return depth


def sync_child_device_disabled_state(
    dev_reg: dr.DeviceRegistry,
    child_dev: dr.DeviceEntry,
    parent_dev: dr.DeviceEntry | None,
) -> bool:
    """Mirror parent device disabled state onto child devices. Return True if changed."""
    if not parent_dev:
        return False
    if parent_dev.disabled_by and child_dev.disabled_by is None:
        dev_reg.async_update_device(
            child_dev.id, disabled_by=dr.DeviceEntryDisabler.INTEGRATION
        )
        return True
    if (
        not parent_dev.disabled_by
        and child_dev.disabled_by is dr.DeviceEntryDisabler.INTEGRATION
    ):
        dev_reg.async_update_device(child_dev.id, disabled_by=None)
        return True
    return False


def sync_device_disabled_cascade(
    dev_reg: dr.DeviceRegistry,
    entry_id: str,
    parent_id: str,
    parent_disabled: bool,
) -> None:
    """Recursively cascade parent disabled state down the via_device_id hierarchy."""
    devices = (
        dr.async_entries_for_config_entry(dev_reg, entry_id)
        if hasattr(dr, "async_entries_for_config_entry")
        else list(dev_reg.devices.values())
    )
    for child in devices:
        if child.via_device_id == parent_id:
            if parent_disabled and child.disabled_by is None:
                dev_reg.async_update_device(
                    child.id, disabled_by=dr.DeviceEntryDisabler.INTEGRATION
                )
                sync_device_disabled_cascade(dev_reg, entry_id, child.id, True)
            elif (
                not parent_disabled
                and child.disabled_by is dr.DeviceEntryDisabler.INTEGRATION
            ):
                dev_reg.async_update_device(child.id, disabled_by=None)
                sync_device_disabled_cascade(dev_reg, entry_id, child.id, False)


def async_setup_device_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_info_map: dict[int, VueDevice],
) -> None:
    """Register physical devices and CT circuit sub-devices in device registry."""
    dev_reg = dr.async_get(hass)

    def _lookup_device_entry(ident: tuple[str, str]) -> dr.DeviceEntry | None:
        """Lookup device entry by identifier respecting Core 2026+ standards."""
        if hasattr(dev_reg, "async_get_device_by_identifier"):
            return dev_reg.async_get_device_by_identifier(ident, entry.entry_id)
        return dev_reg.async_get_device(identifiers={ident})

    # Pass 1: Hardware Physical Devices
    # Sort topologically so root monitors are registered before child monitors/EVSEs
    sorted_physical_devices = sorted(
        device_info_map.values(),
        key=lambda d: get_device_depth(d, device_info_map),
    )
    for device in sorted_physical_devices:
        parent_gid = device.parent_device_gid
        parent_entry = (
            _lookup_device_entry((DOMAIN, f"{parent_gid}-1,2,3"))
            if parent_gid and parent_gid != 0 and parent_gid != device.device_gid
            else None
        )
        child_dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{device.device_gid}-1,2,3")},
            name=device.device_name,
            model=device.model,
            sw_version=device.firmware,
            manufacturer="Emporia",
            serial_number=str(device.manufacturer_id)
            if device.manufacturer_id
            else None,
            via_device_id=parent_entry.id if parent_entry else None,
        )
        sync_child_device_disabled_state(dev_reg, child_dev, parent_entry)

    # Pass 2: CT Circuit Sub-Devices
    for device in device_info_map.values():
        if not device.channels:
            continue
        parent_entry = _lookup_device_entry((DOMAIN, f"{device.device_gid}-1,2,3"))
        for channel in device.channels:
            if channel.has_data is False:
                continue
            if (
                channel.channel_num == "1,2,3"
                or channel.channel_num in SPLIT_PHASE_MAINS_CHANNELS
            ):
                continue

            circuit_name = channel.name
            translation_key = None
            translation_placeholders = None
            if not circuit_name:
                if channel.channel_num.isdigit():
                    circuit_name = f"{device.device_name} circuit {channel.channel_num}"
                    translation_key = "circuit"
                    translation_placeholders = {
                        "device_name": device.device_name,
                        "channel_number": str(channel.channel_num),
                    }
                else:
                    circuit_name = f"{device.device_name} {channel.channel_num}"

            ct_dev = dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{device.device_gid}-{channel.channel_num}")},
                name=circuit_name,
                model=determine_circuit_model(channel, device),
                model_id=None,
                sw_version=device.firmware,
                manufacturer="Emporia",
                via_device_id=parent_entry.id if parent_entry else None,
                translation_key=translation_key,
                translation_placeholders=translation_placeholders,
            )
            sync_child_device_disabled_state(dev_reg, ct_dev, parent_entry)

    # Pass 2b: Link existing unlinked sub-devices to their parent monitor via_device_id
    # Captures preserved unused CT devices from prior versions or user customizations,
    # merged circuits, and virtual circuits.
    existing_devices = (
        dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if hasattr(dr, "async_entries_for_config_entry")
        else list(dev_reg.devices.values())
    )
    for dev_entry in existing_devices:
        for domain, ident in dev_entry.identifiers:
            if domain == DOMAIN and "-" in ident:
                gid_part, ch_part = ident.split("-", 1)
                if (
                    gid_part.isdigit()
                    and ch_part not in ("1,2,3", "1,2", "1,3", "2,3")
                    and not dev_entry.via_device_id
                ):
                    parent = _lookup_device_entry((DOMAIN, f"{gid_part}-1,2,3"))
                    if parent and parent.id != dev_entry.id:
                        dev_reg.async_update_device(
                            dev_entry.id, via_device_id=parent.id
                        )

    # Pass 3: Cascade disabled state across full via_device_id hierarchy (including virtual circuits)
    for device in sorted_physical_devices:
        ident = (DOMAIN, f"{device.device_gid}-1,2,3")
        parent_dev = _lookup_device_entry(ident)
        if parent_dev:
            sync_device_disabled_cascade(
                dev_reg,
                entry.entry_id,
                parent_dev.id,
                bool(parent_dev.disabled_by),
            )


def async_prune_stale_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_info_map: dict[int, VueDevice],
) -> None:
    """Safely remove stale dummy circuit devices from Device Registry."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    inactive_identifiers: set[tuple[str, str]] = set()
    for device in device_info_map.values():
        if not device.channels:
            continue
        for channel in device.channels:
            if channel.channel_num != "1,2,3" and channel.has_data is False:
                inactive_identifiers.add(
                    (DOMAIN, f"{device.device_gid}-{channel.channel_num}")
                )

    if not inactive_identifiers:
        return

    existing_devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    for device_entry in existing_devices:
        matching_inactive = any(
            ident in inactive_identifiers for ident in device_entry.identifiers
        )
        if not matching_inactive:
            continue

        # Preserve if user customized the device
        if device_entry.name_by_user is not None:
            _LOGGER.debug(
                "Preserving inactive device %s due to user-customized name",
                device_entry.id,
            )
            continue
        if device_entry.area_id is not None:
            _LOGGER.debug(
                "Preserving inactive device %s due to assigned area",
                device_entry.id,
            )
            continue
        if device_entry.labels:
            _LOGGER.debug(
                "Preserving inactive device %s due to user labels",
                device_entry.id,
            )
            continue

        # Check if any entity under this device was customized/disabled by the user
        device_entities = er.async_entries_for_device(
            ent_reg, device_entry.id, include_disabled_entities=True
        )
        has_user_disabled = any(
            entity.disabled_by == er.RegistryEntryDisabler.USER
            for entity in device_entities
        )
        if has_user_disabled:
            _LOGGER.debug(
                "Preserving inactive device %s due to user-disabled entity",
                device_entry.id,
            )
            continue

        for entity in device_entities:
            _LOGGER.info(
                "Pruning inactive entity %s from device %s",
                entity.entity_id,
                device_entry.id,
            )
            ent_reg.async_remove(entity.entity_id)

        _LOGGER.info(
            "Pruning inactive uncustomized dummy device %s (%s)",
            device_entry.id,
            device_entry.identifiers,
        )
        dev_reg.async_remove_device(device_entry.id)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
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
                SOLAR_INVERT: conf[SOLAR_INVERT],
            },
        )
    )
    return True


async def async_login_vue(
    loop: asyncio.AbstractEventLoop,
    vue: PyEmVue,
    entry_data: Mapping[str, Any],
) -> bool:
    """Log in to Emporia using the configured authentication method."""
    auth_method = entry_data.get(AUTH_METHOD, AUTH_METHOD_EMAIL_PASSWORD)
    if auth_method == AUTH_METHOD_TOKENS:
        return await loop.run_in_executor(
            None,
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
        return await loop.run_in_executor(None, vue.login_simulator, host)
    return await loop.run_in_executor(
        None,
        partial(vue.login, username=email, password=password),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Emporia Vue from a config entry."""
    global DEVICE_GIDS
    global DEVICE_INFORMATION
    global INVERT_SOLAR
    DEVICE_GIDS = []
    DEVICE_INFORMATION = {}
    LAST_DAY_INTEGRATED_MINUTE.clear()
    LAST_MONTH_INTEGRATED_MINUTE.clear()

    entry_data = {**entry.data, **entry.options}
    _LOGGER.debug(
        "Setting up Emporia Vue with entry data: %s",
        redact_config_data(entry_data),
    )
    if SOLAR_INVERT in entry_data:
        INVERT_SOLAR = entry_data[SOLAR_INVERT]
    vue = PyEmVue()
    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    try:
        result: bool = await async_login_vue(loop, vue, entry_data)
        if not result:
            _LOGGER.error("Failed to login to Emporia Vue")
            raise ConfigEntryAuthFailed("Failed to login to Emporia Vue")
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # pylint: disable=broad-exception-caught
        raise ConfigEntryNotReady(f"Failed to login to Emporia Vue: {err}") from err

    if (
        entry_data.get(AUTH_METHOD) == AUTH_METHOD_TOKENS
        and vue.auth
        and vue.auth.tokens
    ):
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
        get_devices_func = getattr(vue, "get_devices_v1", vue.get_devices)
        devices: list[VueDevice] = await loop.run_in_executor(None, get_devices_func)
        for device in devices:
            for channel in device.channels:
                if not channel.device_gid:
                    channel.device_gid = device.device_gid
            if str(device.device_gid) not in DEVICE_GIDS:
                DEVICE_GIDS.append(str(device.device_gid))
                _LOGGER.info("Adding gid %s to DEVICE_GIDS list", device.device_gid)
                DEVICE_INFORMATION[device.device_gid] = device
            else:
                DEVICE_INFORMATION[device.device_gid].channels += device.channels

        # Cross-resolve parent_device_gid if parent_device_id is provided
        devices_by_id = {
            dev_id: d
            for d in DEVICE_INFORMATION.values()
            if (
                dev_id := (
                    getattr(d, "device_id", None) or getattr(d, "manufacturer_id", None)
                )
            )
        }
        for d in DEVICE_INFORMATION.values():
            parent_id = getattr(d, "parent_device_id", None)
            if (
                (not getattr(d, "parent_device_gid", None) or d.parent_device_gid == 0)
                and parent_id
                and parent_id in devices_by_id
            ):
                d.parent_device_gid = devices_by_id[parent_id].device_gid

        total_channels = 0
        for device in DEVICE_INFORMATION.values():
            total_channels += len(device.channels)
        _LOGGER.info(
            "Found %s Emporia devices with %s total channels",
            len(DEVICE_INFORMATION.keys()),
            total_channels,
        )

        # Safely prune stale uncustomized dummy CT circuit devices from Device Registry
        async_prune_stale_devices(hass, entry, DEVICE_INFORMATION)

        # Pre-register device hierarchy in Device Registry
        async_setup_device_registry(hass, entry, DEVICE_INFORMATION)

        @callback
        def _async_handle_device_registry_update(
            event: Event[dr.EventDeviceRegistryUpdatedData],
        ) -> None:
            """Handle real-time device registry updates to cascade disabled state."""
            if event.data.get("action") != "update":
                return
            changes = event.data.get("changes", {})
            if "disabled_by" not in changes:
                return

            device_id = event.data.get("device_id")
            dev_reg = dr.async_get(hass)
            updated_dev = dev_reg.async_get(device_id)
            dev_entry_id = getattr(updated_dev, "config_entry_id", None)
            dev_entries = getattr(updated_dev, "config_entries", set())
            if not updated_dev or (
                dev_entry_id != entry.entry_id and entry.entry_id not in dev_entries
            ):
                return

            sync_device_disabled_cascade(
                dev_reg,
                entry.entry_id,
                updated_dev.id,
                bool(updated_dev.disabled_by),
            )

        entry.async_on_unload(
            hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, _async_handle_device_registry_update
            )
        )

        async def async_update_data_1min() -> dict:
            """Fetch data from API endpoint at a 1 minute interval.

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            data: dict = await update_sensors(vue, [Scale.MINUTE.value])
            # store this, then have the daily sensors pull from it and integrate
            # then the daily can "true up" hourly (or more frequent) in case it's incorrect
            if data:
                global LAST_MINUTE_DATA
                LAST_MINUTE_DATA = data
            return data

        async def async_update_day_sensors() -> dict:
            global LAST_DAY_UPDATE
            global LAST_DAY_DATA
            now: datetime = datetime.now(UTC)
            if not LAST_DAY_UPDATE or (now - LAST_DAY_UPDATE) > timedelta(minutes=15):
                _LOGGER.info("Updating day sensors")
                LAST_DAY_UPDATE = now
                updated_day_data = await update_sensors(vue, [Scale.DAY.value])
                apply_api_update_debounce(updated_day_data, LAST_DAY_DATA, "day")
                LAST_DAY_DATA = updated_day_data
            else:
                # integrate the minute data
                _LOGGER.info("Integrating minute data into day sensors")
                if LAST_MINUTE_DATA:
                    for identifier, data in LAST_MINUTE_DATA.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        day_id: str = f"{device_gid}-{channel_gid}-{Scale.DAY.value}"
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
                            if not is_newer_sample(
                                LAST_DAY_INTEGRATED_MINUTE, day_id, timestamp
                            ):
                                continue
                            await check_for_midnight(timestamp, int(device_gid), day_id)

                            LAST_DAY_DATA[day_id]["usage"] += data[
                                "usage"
                            ]  # already in kwh
                            LAST_DAY_INTEGRATED_MINUTE[day_id] = timestamp
            return LAST_DAY_DATA

        async def async_update_month_sensors() -> dict:
            global LAST_MONTH_UPDATE
            global LAST_MONTH_DATA
            now: datetime = datetime.now(UTC)
            if not LAST_MONTH_UPDATE or (now - LAST_MONTH_UPDATE) > timedelta(
                minutes=30
            ):
                _LOGGER.info("Updating month sensors")
                LAST_MONTH_UPDATE = now
                updated_month_data = await update_sensors(vue, [Scale.MONTH.value])
                apply_api_update_debounce(
                    updated_month_data,
                    LAST_MONTH_DATA,
                    "month",
                )
                LAST_MONTH_DATA = updated_month_data
            else:
                # integrate the minute data
                _LOGGER.info("Integrating minute data into month sensors")
                if LAST_MINUTE_DATA:
                    for identifier, data in LAST_MINUTE_DATA.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        month_id: str = (
                            f"{device_gid}-{channel_gid}-{Scale.MONTH.value}"
                        )
                        if (
                            data
                            and LAST_MONTH_DATA
                            and month_id in LAST_MONTH_DATA
                            and LAST_MONTH_DATA[month_id]
                            and "usage" in LAST_MONTH_DATA[month_id]
                            and LAST_MONTH_DATA[month_id]["usage"] is not None
                        ):
                            # if we just passed the billing cycle start, reset back to zero
                            timestamp: datetime = data["timestamp"]
                            if not is_newer_sample(
                                LAST_MONTH_INTEGRATED_MINUTE, month_id, timestamp
                            ):
                                continue
                            await check_for_new_month(
                                timestamp, int(device_gid), month_id
                            )

                            LAST_MONTH_DATA[month_id]["usage"] += data[
                                "usage"
                            ]  # already in kwh
                            LAST_MONTH_INTEGRATED_MINUTE[month_id] = timestamp
            return LAST_MONTH_DATA

        coordinator_1min = None
        retry_update_methods: dict[str, TolerantUpdateMethod] = {}
        if ENABLE_1M not in entry_data or entry_data[ENABLE_1M]:
            minute_update_method = TolerantUpdateMethod(
                async_update_data_1min,
                name="Minute telemetry",
                logger=_LOGGER,
                recoverable_exceptions=(UpdateFailed,),
                tolerated_failures=TOLERATED_UPDATE_FAILURES,
            )
            retry_update_methods["minute"] = minute_update_method
            coordinator_1min = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="minute telemetry",
                update_method=minute_update_method,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(minutes=1),
            )
            await coordinator_1min.async_config_entry_first_refresh()
            _LOGGER.debug("1min Update data: %s", coordinator_1min.data)
        coordinator_1mon = None
        if ENABLE_1MON not in entry_data or entry_data[ENABLE_1MON]:
            monthly_update_method = TolerantUpdateMethod(
                async_update_month_sensors,
                name="Monthly telemetry",
                logger=_LOGGER,
                recoverable_exceptions=(UpdateFailed,),
                tolerated_failures=TOLERATED_UPDATE_FAILURES,
            )
            retry_update_methods["monthly"] = monthly_update_method
            coordinator_1mon = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="monthly telemetry",
                update_method=monthly_update_method,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(minutes=1),
            )
            await coordinator_1mon.async_config_entry_first_refresh()
            _LOGGER.debug("1mon Update data: %s", coordinator_1mon.data)

        coordinator_day_sensor = None
        if ENABLE_1D not in entry_data or entry_data[ENABLE_1D]:
            daily_update_method = TolerantUpdateMethod(
                async_update_day_sensors,
                name="Daily telemetry",
                logger=_LOGGER,
                recoverable_exceptions=(UpdateFailed,),
                tolerated_failures=TOLERATED_UPDATE_FAILURES,
            )
            retry_update_methods["daily"] = daily_update_method
            coordinator_day_sensor = DataUpdateCoordinator(
                hass,
                _LOGGER,
                # Name of the data. For logging purposes.
                name="daily telemetry",
                update_method=daily_update_method,
                # Polling interval. Will only be polled if there are subscribers.
                update_interval=timedelta(minutes=1),
            )
            await coordinator_day_sensor.async_config_entry_first_refresh()

        # Check if any devices have outlets or chargers
        has_controllable_devices = any(
            device.outlet or device.ev_charger for device in DEVICE_INFORMATION.values()
        )

        async def async_update_device_status() -> dict[str, Any]:
            """Fetch device status (outlets and chargers)."""
            try:
                data: dict[str, Any] = {}
                outlets: list[OutletDevice]
                chargers: list[ChargerDevice]

                outlets, chargers = await hass.async_add_executor_job(
                    vue.get_devices_status
                )

                if outlets:
                    for outlet in outlets:
                        data[str(outlet.device_gid)] = outlet
                if chargers:
                    for charger in chargers:
                        data[str(charger.device_gid)] = charger
                return data
            except Exception as err:
                raise UpdateFailed(
                    f"Error communicating with Emporia API: {err}"
                ) from err

        coordinator_device_status = None
        if has_controllable_devices:
            coordinator_device_status = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name="device_status",
                update_method=async_update_device_status,
                update_interval=timedelta(minutes=1),
            )
            await coordinator_device_status.async_config_entry_first_refresh()

        # Setup custom services
        async def handle_set_charger_current(call) -> None:
            """Handle setting the EV Charger current."""
            _LOGGER.debug(
                "executing set_charger_current: %s %s",
                str(call.service),
                str(call.data),
            )
            current = call.data.get("current")
            current = int(current)
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

            unique_entity_id: str = charger_entity.unique_id
            gid_match: re.Match[str] | None = re.search(r"\d+", unique_entity_id)
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
                    "Set Charging Current called on invalid device with entity id"
                    f" {charger_entity.entity_id} (unique id {unique_entity_id})"
                )

            state = hass.states.get(charger_entity.entity_id)
            _LOGGER.info("State is %s", str(state))
            if not state:
                raise HomeAssistantError(
                    f"Could not find state for entity {charger_entity.entity_id}"
                )
            charger_info: VueDevice = DEVICE_INFORMATION[charger_gid]
            if charger_info.ev_charger is None:
                raise HomeAssistantError(
                    f"Could not find charger info for device {charger_gid}"
                )
            # Scale the current to a minimum of 6 amps and max of the circuit max
            current: int = max(6, current)
            current = min(current, charger_info.ev_charger.max_charging_rate)
            _LOGGER.info(
                "Setting charger %s to current of %d amps", charger_gid, current
            )

            try:
                updated_charger: ChargerDevice = await loop.run_in_executor(
                    None,
                    vue.update_charger,
                    charger_info.ev_charger,
                    state.state == "on",
                    current,
                )
                DEVICE_INFORMATION[charger_gid].ev_charger = updated_charger
                # update the state of the charger entity using the updated data
                state: State | None = hass.states.get(charger_entity.entity_id)
                if state:
                    new_state: str = "on" if updated_charger.charger_on else "off"
                    new_attributes: dict = state.attributes.copy()
                    new_attributes["charging_rate"] = updated_charger.charging_rate
                    # good enough for now, update the state in the registry
                    hass.states.async_set(
                        charger_entity.entity_id, new_state, new_attributes
                    )

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
        ) from err

    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: vue,
        "coordinator_1min": coordinator_1min,
        "coordinator_1mon": coordinator_1mon,
        "coordinator_day_sensor": coordinator_day_sensor,
        "coordinator_device_status": coordinator_device_status,
        "device_information": DEVICE_INFORMATION,
        "retry_update_methods": retry_update_methods,
    }

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as err:
        _LOGGER.warning("Error setting up platforms: %s", err)
        raise ConfigEntryNotReady(f"Error setting up platforms: {err}") from err

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok: bool = all(
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


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting inactive or orphaned devices from the device registry."""
    vue_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    device_info_map: dict[int, VueDevice] = vue_data.get("device_information", {})

    active_idents: set[str] = set()
    for dev in device_info_map.values():
        active_idents.add(f"{dev.device_gid}-1,2,3")
        for ch in getattr(dev, "channels", []):
            if getattr(ch, "has_data", None) is not False:
                active_idents.add(f"{dev.device_gid}-{ch.channel_num}")

    for dom, ident in device_entry.identifiers:
        if dom == DOMAIN and ident in active_idents:
            dev_name = device_entry.name_by_user or device_entry.name or "device"
            raise HomeAssistantError(
                f"Cannot remove active device '{dev_name}'. Only inactive or decommissioned devices can be deleted."
            )

    return True


def fetch_device_usages(
    vue: PyEmVue,
    gids: list[str],
    instant: datetime,
    scale: str,
    energy_unit: Any = None,
) -> dict[int, VueUsageDevice]:
    """Fetch device usages with v1 and legacy fallbacks."""
    try:
        scale_val = Scale(scale)
    except ValueError:
        scale_val = scale

    if hasattr(vue, "get_device_list_usage_v1"):
        try:
            kwargs: dict[str, Any] = {}
            if energy_unit is not None:
                kwargs["energy_unit"] = energy_unit
            res = vue.get_device_list_usage_v1(
                device_gids=gids,
                instant=instant,
                scale=scale_val,
                **kwargs,
            )
            if res:
                return res
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("get_device_list_usage_v1 failed, trying fallback: %s", err)

    if energy_unit == Unit.VOLTS:
        return {}

    for method_name in ("get_device_list_usage", "get_devices_usage"):
        method = getattr(vue, method_name, None)
        if method:
            try:
                res = method(gids, instant, scale)
                if res:
                    return res
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s failed: %s", method_name, err)

    return {}


async def update_sensors(vue: PyEmVue, scales: list[str]) -> dict:
    """Fetch data from API endpoint."""
    try:
        # Note: asyncio.TimeoutError and aiohttp.ClientError are already
        # handled by the data update coordinator.
        data: dict = {}
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        for scale in scales:
            utcnow: datetime = datetime.now(UTC)
            usage_dict: dict[int, VueUsageDevice] = await loop.run_in_executor(
                None, fetch_device_usages, vue, DEVICE_GIDS, utcnow, scale
            )
            if not usage_dict:
                _LOGGER.warning(
                    "No channels found during update for scale %s. Retrying", scale
                )
                usage_dict = await loop.run_in_executor(
                    None, fetch_device_usages, vue, DEVICE_GIDS, utcnow, scale
                )
            if usage_dict:
                flattened, data_time = flatten_usage_data(usage_dict, scale)
                flattened_voltage: dict[str, VueDeviceChannelUsage] = {}
                if scale == Scale.MINUTE.value and hasattr(
                    vue, "get_device_list_usage_v1"
                ):
                    try:
                        voltage_dict = await loop.run_in_executor(
                            None,
                            fetch_device_usages,
                            vue,
                            DEVICE_GIDS,
                            utcnow,
                            scale,
                            Unit.VOLTS,
                        )
                        if voltage_dict:
                            flattened_voltage, _ = flatten_usage_data(
                                voltage_dict, scale
                            )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Failed to fetch voltage telemetry: %s", err)

                await parse_flattened_usage_data(
                    flattened,
                    scale,
                    data,
                    utcnow,
                    data_time,
                    flattened_voltage,
                )
            else:
                raise UpdateFailed(f"No channels found during update for scale {scale}")

        return data
    except Exception as err:
        _LOGGER.debug("Error communicating with Emporia API", exc_info=True)
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
    flattened_voltage: dict[str, VueDeviceChannelUsage] | None = None,
) -> None:
    """Loop through the device list and find the corresponding update data."""
    unused_data: dict[str, VueDeviceChannelUsage] = flattened_data.copy()
    for gid, info in DEVICE_INFORMATION.items():
        local_time: datetime = await change_time_to_local(data_time, info.time_zone)
        requested_time_local: datetime = await change_time_to_local(
            requested_time, info.time_zone
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
            if info_channel.has_data is False:
                unused_data.pop(identifier, None)
                continue
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
                fixed_usage = handle_none_usage(scale, identifier)
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
                channel_num, fixed_usage, bidirectional, is_solar, INVERT_SOLAR
            )

            channel_voltage: float | None = None
            if flattened_voltage and identifier in flattened_voltage:
                v_chan = flattened_voltage[identifier]
                if v_chan.usage is not None:
                    channel_voltage = float(v_chan.usage)

            data[identifier] = {
                "device_gid": gid,
                "channel_num": channel_num,
                "usage": fixed_usage,
                "voltage": channel_voltage,
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
            channels_were_added |= await handle_special_channels_for_device(channel)
            # we'll also need to register these entities I think. They might show up
            # automatically on the first run When we're done handling the unused data
            # we need to rerun the update
        if channels_were_added:
            _LOGGER.info("Rerunning update due to added channels")
            await parse_flattened_usage_data(
                flattened_data,
                scale,
                data,
                requested_time,
                data_time,
                flattened_voltage,
            )


async def handle_special_channels_for_device(channel: VueDeviceChannel) -> bool:
    """Handle the special channels for a device, if they exist."""
    if channel.device_gid in DEVICE_INFORMATION:
        device_info: VueDevice = DEVICE_INFORMATION[channel.device_gid]
        # if channel.channel_num in [
        #     "MainsFromGrid",
        #     "MainsToGrid",
        #     "Balance",
        #     "TotalUsage",
        # ]:
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
    """If the channel is not '1,2,3', split-phase mains legs, or 'Balance' we need it to be positive.

    Solar circuits are up to the user to decide. Positive is recommended for the energy dashboard.

    (see https://github.com/magico13/ha-emporia-vue/issues/57)
    """
    if is_solar:
        # Energy dashboard wants solar to be positive, Emporia usually provides negative
        if usage and invert_solar:
            return -1 * usage
        return usage

    if (
        usage
        and not bidirectional
        and channel_num
        not in [
            "1,2,3",
            "Balance",
            *SPLIT_PHASE_MAINS_CHANNELS,
        ]
    ):
        # With bidirectionality, we need to also check if bidirectional. If yes,
        # we either don't abs, or we flip the sign.
        return abs(usage)
    return usage


async def change_time_to_local(time: datetime, tz_string: str) -> datetime:
    """Change the datetime to the provided timezone, if not already."""
    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    tz_info: tzinfo | None = await loop.run_in_executor(
        None, dateutil.tz.gettz, tz_string
    )
    if not time.tzinfo or time.tzinfo.utcoffset(time) is None:
        # unaware, assume it's already utc
        time = time.replace(tzinfo=UTC)
    return time.astimezone(tz_info)


async def check_for_midnight(timestamp: datetime, device_gid: int, day_id: str):
    """If midnight has recently passed, reset the LAST_DAY_DATA for Day sensors to zero."""
    if device_gid in DEVICE_INFORMATION:
        device_info: VueDevice = DEVICE_INFORMATION[device_gid]
        local_time: datetime = await change_time_to_local(
            timestamp, device_info.time_zone
        )
        local_midnight: datetime = local_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        last_reset = LAST_DAY_DATA[day_id]["reset"]
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
            LAST_DAY_DATA[day_id]["usage"] = 0
            LAST_DAY_DATA[day_id]["reset"] = local_midnight


async def check_for_new_month(timestamp: datetime, device_gid: int, month_id: str):
    """If a new billing cycle has started, reset the LAST_MONTH_DATA for Month sensors to zero."""
    if device_gid in DEVICE_INFORMATION:
        device_info: VueDevice = DEVICE_INFORMATION[device_gid]
        local_time: datetime = await change_time_to_local(
            timestamp, device_info.time_zone
        )
        current_reset: datetime = determine_reset_datetime(
            local_time,
            device_info.billing_cycle_start_day,
            True,
        )
        last_reset = LAST_MONTH_DATA[month_id]["reset"]
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
            LAST_MONTH_DATA[month_id]["usage"] = 0
            LAST_MONTH_DATA[month_id]["reset"] = current_reset


def determine_reset_datetime(
    local_time: datetime, monthly_cycle_start: int, is_month: bool
) -> datetime:
    """Determine the last reset datetime (aware) based on the passed time and cycle start date."""
    reset_datetime: datetime = local_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if is_month:
        # Month should use the most recent billing_cycle_start_day midnight.
        # Never return a future reset datetime.
        last_day_this_month = calendar.monthrange(
            reset_datetime.year, reset_datetime.month
        )[1]
        target_day_this_month = min(monthly_cycle_start, last_day_this_month)
        candidate_this_month = reset_datetime.replace(day=target_day_this_month)

        if local_time >= candidate_this_month:
            reset_datetime = candidate_this_month
        else:
            previous_month = reset_datetime - dateutil.relativedelta.relativedelta(
                months=1
            )
            last_day_previous_month = calendar.monthrange(
                previous_month.year, previous_month.month
            )[1]
            target_day_previous_month = min(
                monthly_cycle_start, last_day_previous_month
            )
            reset_datetime = previous_month.replace(day=target_day_previous_month)
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
