"""Platform for sensor integration."""
from datetime import timedelta
import logging

import asyncio
import async_timeout

from homeassistant.const import DEVICE_CLASS_POWER, POWER_WATT, ENERGY_WATT_HOUR, ENERGY_KILO_WATT_HOUR
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, VUE_DATA

from pyemvue import pyemvue
from pyemvue.enums import Scale
from pyemvue.device import VueDevice, VueDeviceChannel, VuewDeviceChannelUsage

_LOGGER = logging.getLogger(__name__)


"""
data model
[
    {
        "device_gid": 1234,
        "channel_num": 5678,
        "usage": 12.34,
        "scale": "1MIN"
    },
    {}
]
"""

#def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the sensor platform."""
    vue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]

    # Populate the initial device information? ie get_devices() and populate_device_properties()

    async def async_update_data():
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            data = {}
            scales = [
                Scale.MINUTE.value,
                Scale.DAY.value,
                Scale.MONTH.value
            ]
            for scale in scales:
                loop = asyncio.get_event_loop()
                channels = await loop.run_in_executor(None, vue.get_recent_usage, scale)
                if channels:
                    for channel in channels:
                        id = '{0}-{1}-{2}'.format(channel.device_gid, channel.channel_num, scale)
                        data[id] = {
                                "device_gid": channel.device_gid,
                                "channel_num": channel.channel_num,
                                "usage": round(channel.usage),
                                "scale": scale,
                                "channel": channel
                            }
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Emporia API: {err}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name="sensor",
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=timedelta(seconds=60),
    )

    await coordinator.async_refresh()

    async_add_entities(
        CurrentVuePowerSensor(coordinator, id) for idx, id in enumerate(coordinator.data)
    )

class CurrentVuePowerSensor(CoordinatorEntity, Entity):
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, id):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        #self._state = coordinator.data[index]['usage']
        self._id = id
        self._scale = coordinator.data[id]["scale"]
        #self._device = device
        self._channel = coordinator.data[id]["channel"]
        dName = 'test'#self._channel.name# or device.device_name
        self._name = f'Power {dName} {self._channel.channel_num} {self._scale}'
        self._iskwh = (self._scale != Scale.MINUTE.value and self._scale != Scale.SECOND.value and self._scale != Scale.MINUTES_15.value)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        usage = self.coordinator.data[self._id]['usage']
        if self._iskwh:
            usage /= 1000.0
        return usage

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        if self._iskwh:
            return ENERGY_KILO_WATT_HOUR
        else:
            return POWER_WATT
    
    @property
    def device_class(self):
        """The type of sensor"""
        return DEVICE_CLASS_POWER

    @property
    def unique_id(self):
        """Unique ID for the sensor"""
        if self._scale == Scale.MINUTE.value:
            return f"sensor.emporia_vue.instant.{self._channel.device_gid}-{self._channel.channel_num}"
        else:
            return f"sensor.emporia_vue.{self._scale}.{self._channel.device_gid}-{self._channel.channel_num}"
