"""Platform for sensor integration."""
from homeassistant.components.sensor import STATE_CLASS_TOTAL_INCREASING, STATE_CLASS_MEASUREMENT, SensorEntity
import logging

from homeassistant.const import (
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_POWER,
    POWER_WATT,
    ENERGY_KILO_WATT_HOUR,
)

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import DOMAIN

from pyemvue.enums import Scale

_LOGGER = logging.getLogger(__name__)

# def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the sensor platform."""
    coordinator_1min = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1min"]
    coordinator_1hr = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1hr"]
    coordinator_day_sensor = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_day_sensor"
    ]

    _LOGGER.info(hass.data[DOMAIN][config_entry.entry_id])

    if coordinator_1min:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_1min, id)
            for _, id in enumerate(coordinator_1min.data)
        )

    if coordinator_1hr:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_1hr, id)
            for _, id in enumerate(coordinator_1hr.data)
        )

    if coordinator_day_sensor:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_day_sensor, id)
            for _, id in enumerate(coordinator_day_sensor.data)
        )


class CurrentVuePowerSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, id):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = id
        self._scale = coordinator.data[id]["scale"]
        device_gid = coordinator.data[id]["device_gid"]
        channel_num = coordinator.data[id]["channel_num"]
        self._device = coordinator.data[id]["info"]
        self._channel = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    self._channel = channel
                    break
        if self._channel is None:
            _LOGGER.warn(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        dName = self._device.device_name
        if self._channel.name and self._channel.name not in ["Main", "Balance"]:
            dName = self._channel.name
        self._name = f"{dName} {channel_num} {self._scale}"
        self._iskwh = self.scale_is_energy()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        usage = self.coordinator.data[self._id]["usage"]
        return self.scale_usage(usage)

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
        if self._iskwh:
            return DEVICE_CLASS_ENERGY
        else:
            return DEVICE_CLASS_POWER

    @property
    def state_class(self):
        """Type of state."""
        if self._iskwh:
            return STATE_CLASS_TOTAL_INCREASING
        else:
            return STATE_CLASS_MEASUREMENT

    @property
    def last_reset(self):
        """The time when the daily/monthly sensor was reset. Midnight local time."""
        return self.coordinator.data[self._id]["reset"]

    @property
    def unique_id(self):
        """Unique ID for the sensor"""
        if self._scale == Scale.MINUTE.value:
            return f"sensor.emporia_vue.instant.{self._channel.device_gid}-{self._channel.channel_num}"
        else:
            return f"sensor.emporia_vue.{self._scale}.{self._channel.device_gid}-{self._channel.channel_num}"

    @property
    def device_info(self):
        dName = self._channel.name or self._device.device_name

        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (
                    DOMAIN,
                    "{0}-{1}".format(
                        self._device.device_gid, self._channel.channel_num
                    ),
                )
            },
            "name": dName,
            "model": self._device.model,
            "sw_version": self._device.firmware,
            # "via_device": self._device.device_gid # might be able to map the extender, nested outlets
        }

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = round(60 * 1000 * usage)  # convert from kwh to w rate
        elif self._scale == Scale.SECOND.value:
            usage = round(3600 * 1000 * usage)  # convert to rate
        elif self._scale == Scale.MINUTES_15.value:
            usage = round(
                4 * 1000 * usage
            )  # this might never be used but for safety, convert to rate
        else:
            usage = round(usage, 3)
        return usage

    def scale_is_energy(self):
        """Returns True if the scale is an energy unit instead of power (hour and bigger)"""
        return (
            self._scale != Scale.MINUTE.value
            and self._scale != Scale.SECOND.value
            and self._scale != Scale.MINUTES_15.value
        )
