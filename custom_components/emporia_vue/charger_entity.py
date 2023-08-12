import logging
from typing import Any, Optional

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import DOMAIN

from pyemvue import pyemvue
from pyemvue.device import VueDevice, ChargerDevice

_LOGGER = logging.getLogger(__name__)


class EmporiaChargerEntity(CoordinatorEntity):
    """Emporia Charger Entity"""

    def __init__(
        self,
        coordinator,
        vue: pyemvue.PyEmVue,
        device: VueDevice,
        units: Optional[str],
        device_class: str,
        enabled_default=True,
    ) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._device = device
        self._vue = vue
        self._enabled_default = enabled_default

        self._attr_unit_of_measurement = units
        self._attr_device_class = device_class
        self._attr_name = device.device_name

    @property
    def entity_registry_enabled_default(self):
        return self._enabled_default

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: ChargerDevice = self._coordinator.data[self._device.device_gid]
        if data:
            return {
                "charging_rate": data.charging_rate,
                "max_charging_rate": data.max_charging_rate,
                "status": data.status,
                "message": data.message,
                "fault_text": data.fault_text,
                "icon_name": data.icon,
                "icon_label": data.icon_label,
                "icon_detail_text": data.icon_detail_text,
            }
        return {}

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger"""
        return f"charger.emporia_vue.{self._device.device_gid}"

    @property
    def device_info(self):
        """Return the device information."""
        return {
            "identifiers": {(DOMAIN, "{0}-1,2,3".format(self._device.device_gid))},
            "name": self._device.device_name + "-1,2,3",
            "model": self._device.model,
            "sw_version": self._device.firmware,
            "manufacturer": "Emporia",
        }

    @property
    def available(self):
        """Return True if entity is available."""
        return self._device
