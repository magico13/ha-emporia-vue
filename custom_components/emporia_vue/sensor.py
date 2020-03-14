"""Platform for sensor integration."""
from homeassistant.const import DEVICE_CLASS_POWER, POWER_WATT, ENERGY_WATT_HOUR, ENERGY_KILO_WATT_HOUR
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VUE_DATA

from pyemvue import pyemvue
from pyemvue.enums import Scale
from pyemvue.device import VueDevice, VueDeviceChannel, VuewDeviceChannelUsage

#def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(hass, config_entry, add_entities):
    """Set up the sensor platform."""
    vue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]
    vue_devices = vue.get_devices()

    # Add a sensor for each device channel
    devices = []
    for device in vue_devices:
        for channel in device.channels:
            devices.append(CurrentVuePowerSensor(vue, channel))

    add_entities(devices)


class CurrentVuePowerSensor(Entity):
    """Representation of a Vue Sensor's current power."""

    def __init__(self, vue, channel):
        """Initialize the sensor."""
        self._state = None
        self._vue = vue
        self._channel = channel

    @property
    def name(self):
        """Return the name of the sensor."""
        return f'Power {self._channel.device_gid} {self._channel.channel_num}'

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return POWER_WATT
    
    @property
    def device_class(self):
        """The type of sensor"""
        return DEVICE_CLASS_POWER

    @property
    def unique_id(self):
        """Unique ID for the sensor"""
        return f"sensor.emporia_vue.instant.{self._channel.device_gid}-{self._channel.channel_num}"

    def update(self):
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        """
        gid = self._channel.device_gid
        num = self._channel.channel_num

        # TODO: each sensor shouldn't do this separately
        channels = self._vue.get_recent_usage(scale=Scale.MINUTE.value)
        if channels:
            for channel in channels:
                if channel.device_gid == gid and channel.channel_num == num:
                    self._state = round(channel.usage)
                    return

        self._state = None
        return
