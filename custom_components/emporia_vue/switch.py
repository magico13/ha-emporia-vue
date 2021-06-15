"""Platform for switch integration."""
from datetime import timedelta
import logging

import asyncio
import async_timeout

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, VUE_DATA

from pyemvue import pyemvue
from pyemvue.device import OutletDevice

_LOGGER = logging.getLogger(__name__)

device_information = {} # data is the populated device objects

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the sensor platform."""
    vue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]

    loop = asyncio.get_event_loop()
    devices = await loop.run_in_executor(None, vue.get_devices)
    for device in devices:
        if device.outlet is not None:
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
            outlets = await loop.run_in_executor(None, vue.get_outlets)
            if outlets:
                for outlet in outlets:
                    data[outlet.device_gid] = outlet
            return data
        except Exception as err:
            raise UpdateFailed(f'Error communicating with Emporia API: {err}')

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name='switch',
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=timedelta(minutes=5),
    )

    await coordinator.async_refresh()

    async_add_entities(
        EmporiaOutletSwitch(coordinator, vue, id) for idx, id in enumerate(coordinator.data)
    )

class EmporiaOutletSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of an Emporia Smart Outlet state"""

    def __init__(self, coordinator, vue, id):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        #self._state = coordinator.data[index]['usage']
        self._vue = vue
        self._device_gid = id
        self._device = device_information[id]
        self._name = f'Switch {self._device.device_name}'

    @property
    def name(self):
        """Return the name of the switch."""
        return self._name

    @property
    def is_on(self):
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].outlet_on

    # @property
    # def current_power_w(self):
    #     """Return the current power consumption of the switch."""
    #     return None # so this one is sorta funny because there are separate energy sensors

    # @property
    # def today_energy_kwh(self):
    #     """Return the power consumption today for the switch."""
    #     return None # so this one is sorta funny because there are separate energy sensors

    # @property
    # def is_standby(self):
    #     """Indicate if the device connected to the switch is currently in standby."""
    #     return None # Could apply a semi-arbitrary limit of like 5 watts for this

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._vue.update_outlet, self.coordinator.data[self._device_gid], True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._vue.update_outlet, self.coordinator.data[self._device_gid], False)
        await self.coordinator.async_request_refresh()

    @property
    def unique_id(self):
        """Unique ID for the switch"""
        return f'switch.emporia_vue.{self._device_gid}'

    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, '{0}-1,2,3'.format(self._device_gid))
            },
            "name": self._device.device_name+'-1,2,3',
            "model": self._device.model,
            "sw_version": self._device.firmware,
            #"via_device": self._device.device_gid # might be able to map the extender, nested outlets
        }
