"""Platform for sensor integration."""
from datetime import datetime
import logging
from typing import Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyemvue.device import VueDevice, VueDeviceChannel
from pyemvue.enums import Scale

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback
    ) -> None:
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
        final_channel: Optional[VueDeviceChannel] = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    final_channel = channel
                    break
        if final_channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        self._channel: VueDeviceChannel = final_channel
        self._iskwh = self.scale_is_energy()

        self._attr_has_entity_name = True
        self._attr_suggested_display_precision = 3
        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_name = f"Energy {self._scale}"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_name = f"Power {self._scale}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        device_name = self._channel.name or self._device.device_name
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self._device.device_gid}-{self._channel.channel_num}")
            },
            name=device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )

    @property
    def last_reset(self) -> datetime | None:
        """The time when the daily/monthly sensor was reset. Midnight local time."""
        if self._id in self.coordinator.data:
            return self.coordinator.data[self._id]["reset"]
        return None

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            usage = self.coordinator.data[self._id]["usage"]
            return self.scale_usage(usage) if usage is not None else None
        return None

    @property
    def unique_id(self) -> str:
        """Unique ID for the sensor."""
        if self._scale == Scale.MINUTE.value:
            return f"sensor.emporia_vue.instant.{self._channel.device_gid}-{self._channel.channel_num}"
        return f"sensor.emporia_vue.{self._scale}.{self._channel.device_gid}-{self._channel.channel_num}"

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = 60 * 1000 * usage  # convert from kwh to w rate
        elif self._scale == Scale.SECOND.value:
            usage = 3600 * 1000 * usage  # convert to rate
        elif self._scale == Scale.MINUTES_15.value:
            usage = 4 * 1000 * usage # this might never be used but for safety, convert to rate
        return usage

    def scale_is_energy(self):
        """Return True if the scale is an energy unit instead of power."""
        return self._scale not in (
            Scale.MINUTE.value,
            Scale.SECOND.value,
            Scale.MINUTES_15.value,
        )
