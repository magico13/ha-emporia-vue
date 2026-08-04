"""Emporia Charger Entity."""

from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice, VueDevice

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN


class EmporiaChargerEntity(CoordinatorEntity):
    """Emporia Charger Entity."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        device: VueDevice,
        units: str | None,
        device_class: str,
        enabled_default=True,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device: VueDevice = device
        self._device_gid = str(device.device_gid)
        self._vue: PyEmVue = vue
        self._enabled_default: bool = enabled_default

        self._attr_native_unit_of_measurement = units
        self._attr_device_class = device_class
        self._attr_has_entity_name = True
        self._attr_name = None

    @property
    def available(self) -> bool:
        """Return True if the coordinator has current status data for this device."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._device_gid in self.coordinator.data
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether the entity should be enabled when first added to the entity registry."""
        return self._enabled_default

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the device."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
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
        """Unique ID for the charger."""
        return f"charger.emporia_vue.{self._device_gid}"

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
