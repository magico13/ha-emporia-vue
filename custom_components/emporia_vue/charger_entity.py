import logging
from datetime import datetime
from typing import Callable, List

from homeassistant.const import ENERGY_WATT_HOUR, POWER_WATT
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_registry import async_entries_for_device
from homeassistant.util import dt

from .const import DOMAIN, VUE_DATA

from pyemvue import pyemvue
from pyemvue.device import VueDevice, ChargerDevice

_LOGGER = logging.getLogger(__name__)


class EmporiaChargerEntity(Entity):
    """Emporia Charger Entity"""

    def __init__(
        self,
        data,
        device: VueDevice,
        units: str,
        device_class: str,
        enabled_default=True,
    ):
        self._device = device
        self.data = data
        self._enabled_default = enabled_default
        self._units = units
        self._device_class = device_class

        self._charger = device.ev_charger
        self._name = device.device_name

    @property
    def entity_registry_enabled_default(self):
        return self._enabled_default

    @property
    def name(self):
        """Name of the entity."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger"""
        return f"charger.emporia_vue.{self._device.device_gid}"

    @property
    def device_info(self):
        """Return the device information."""
        return {
            "identifiers": (DOMAIN, str(self._device.device_gid)),
            "name": self._device.device_name,
            "model": self._device.model,
            "sw_version": self._device.firmware,
            "manufacturer": "Emporia",
        }

    @property
    def native_unit_of_measurement(self):
        """Return the native unit of measurement of this entity, if any."""
        return self._units

    @property
    def available(self):
        """Return True if entity is available."""
        return self._device.connected

    @property
    def device_class(self):
        """Device class of sensor."""
        return self._device_class

    @property
    def should_poll(self):
        """No polling needed."""
        return False
