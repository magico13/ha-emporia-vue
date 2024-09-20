"""Platform for switch integration."""
import asyncio
from datetime import timedelta
import logging
from typing import Any

from requests import exceptions

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from pyemvue.device import ChargerDevice, OutletDevice, VueDevice

from .charger_entity import EmporiaChargerEntity
from .const import DOMAIN, VUE_DATA

_LOGGER = logging.getLogger(__name__)

device_information: dict[int, VueDevice] = {}  # data is the populated device objects


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    vue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]

    loop = asyncio.get_event_loop()
    devices: list[VueDevice] = await loop.run_in_executor(None, vue.get_devices)
    for device in devices:
        if device.outlet or device.ev_charger:
            await loop.run_in_executor(None, vue.populate_device_properties, device)
            device_information[device.device_gid] = device

    async def async_update_data():
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            data = {}
            loop = asyncio.get_event_loop()
            outlets: list[OutletDevice]
            chargers: list[ChargerDevice]
            (outlets, chargers) = await loop.run_in_executor(
                None, vue.get_devices_status
            )
            if outlets:
                for outlet in outlets:
                    data[outlet.device_gid] = outlet
            if chargers:
                for charger in chargers:
                    data[charger.device_gid] = charger
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name="switch",
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=timedelta(minutes=1),
    )

    await coordinator.async_refresh()

    switches = []
    for _, gid in enumerate(coordinator.data):
        if gid not in device_information:
            continue
        if device_information[gid].outlet:
            switches.append(EmporiaOutletSwitch(coordinator, vue, gid))
        elif  device_information[gid].ev_charger:
            switches.append(
                EmporiaChargerSwitch(
                    coordinator,
                    vue,
                    device_information[gid],
                    None,
                    SwitchDeviceClass.OUTLET,
                )
            )

    async_add_entities(switches)


class EmporiaOutletSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of an Emporia Smart Outlet state."""

    def __init__(self, coordinator, vue, gid) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        # self._state = coordinator.data[index]['usage']
        self._vue = vue
        self._device_gid = gid
        self._device = device_information[gid]
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_device_class = SwitchDeviceClass.OUTLET

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._vue.update_outlet, self.coordinator.data[self._device_gid], True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._vue.update_outlet,
            self.coordinator.data[self._device_gid],
            False,
        )
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_gid}-1,2,3")},
            name=self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].outlet_on

    @property
    def unique_id(self) -> str:
        """Unique ID for the switch."""
        return f"switch.emporia_vue.{self._device_gid}"

class EmporiaChargerSwitch(EmporiaChargerEntity, SwitchEntity):
    """Representation of an Emporia Charger switch state."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the charger on."""
        await self._update_switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the charger off."""
        await self._update_switch(False)

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device.device_gid].charger_on

    async def _update_switch(self, on: bool):
        """Update the switch."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                self._vue.update_charger,
                self._coordinator.data[self._device.device_gid],
                on,
            )
        except exceptions.HTTPError as err:
            _LOGGER.error(
                "Error updating charger status: %s \nResponse body: %s",
                err,
                err.response.text,
            )
            raise
        await self._coordinator.async_request_refresh()
