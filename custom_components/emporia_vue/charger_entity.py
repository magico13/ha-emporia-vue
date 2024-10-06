"""Emporia Charger Entity."""

from functools import cached_property
from typing import Any, Optional

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyemvue import pyemvue
from pyemvue.device import ChargerDevice, VueDevice

from .const import DOMAIN


class EmporiaChargerEntity(CoordinatorEntity):
    """Emporia Charger Entity."""

    def __init__(
        self,
        coordinator,
        vue: pyemvue.PyEmVue,
        device: VueDevice,
        units: Optional[str],
        device_class: str,
        enabled_default=True,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._device: VueDevice = device
        self._vue: pyemvue.PyEmVue = vue
        self._enabled_default: bool = enabled_default

        self._attr_unit_of_measurement = units
        self._attr_device_class = device_class
        self._attr_has_entity_name = True
        self._attr_name = None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._device is not None

    @cached_property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether the entity should be enabled when first added to the entity registry."""
        return self._enabled_default

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the device."""
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

    @cached_property
    def unique_id(self) -> str:
        """Unique ID for the charger."""
        return f"charger.emporia_vue.{self._device.device_gid}"

    @cached_property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device.device_gid}-1,2,3")},
            name=self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )
