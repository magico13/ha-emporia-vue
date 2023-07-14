"""Platform for sensor integration."""
from typing import Optional
from homeassistant.components.sensor import (
    SensorStateClass,
    SensorDeviceClass,
    SensorEntity,
)
import logging

from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import DOMAIN

from pyemvue.enums import Scale
from pyemvue.device import VueDevice, VueDeviceChannel

_LOGGER = logging.getLogger(__name__)


# def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the sensor platform."""
    coordinator_1min = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1min"]
    coordinator_1mon = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1mon"]
    coordinator_day_sensor = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_day_sensor"
    ]

    _LOGGER.info(hass.data[DOMAIN][config_entry.entry_id])

    if coordinator_1min:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_1min, id)
            for _, id in enumerate(coordinator_1min.data)
        )

    if coordinator_1mon:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_1mon, id)
            for _, id in enumerate(coordinator_1mon.data)
        )

    if coordinator_day_sensor:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_day_sensor, id)
            for _, id in enumerate(coordinator_day_sensor.data)
        )


class CurrentVuePowerSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, identifier) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._scale: str = coordinator.data[identifier]["scale"]
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]
        self._channel: Optional[VueDeviceChannel] = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    self._channel = channel
                    break
        if self._channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        device_name = self._device.device_name
        if self._channel.name and self._channel.name not in [
            "Main",
            "Balance",
            "TotalUsage",
            "MainsToGrid",
            "MainsFromGrid",
        ]:
            device_name = self._channel.name
        self._name = f"{device_name} {channel_num} {self._scale}"
        self._iskwh = self.scale_is_energy()

        self._attr_name = self._name
        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            usage = self.coordinator.data[self._id]["usage"]
            return self.scale_usage(usage) if usage is not None else None
        return None

    @property
    def last_reset(self):
        """The time when the daily/monthly sensor was reset. Midnight local time."""
        if self._id in self.coordinator.data:
            return self.coordinator.data[self._id]["reset"]
        return None

    @property
    def unique_id(self):
        """Unique ID for the sensor"""
        if self._scale == Scale.MINUTE.value:
            return f"sensor.emporia_vue.instant.{self._channel.device_gid}-{self._channel.channel_num}"
        return f"sensor.emporia_vue.{self._scale}.{self._channel.device_gid}-{self._channel.channel_num}"

    @property
    def device_info(self):
        device_name = self._channel.name or self._device.device_name
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
            "name": device_name,
            "model": self._device.model,
            "sw_version": self._device.firmware,
            "manufacturer": "Emporia"
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
        return self._scale not in (
            Scale.MINUTE.value,
            Scale.SECOND.value,
            Scale.MINUTES_15.value,
        )
