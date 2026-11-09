"""Emporia Charger Entity."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from pyemvue import pyemvue
from pyemvue.device import ChargerDevice, VueDevice

from .const import DOMAIN


class EmporiaLocationEntityMixin:
    """Mixin to provide dynamic location metadata to entities."""

    _device: Any | None = None
    _explicit_site_map: dict[int, Any] | None = None

    @property
    def _site_map(self) -> dict[int, Any]:
        """Return cached site map from explicit setting, coordinator, or hass data."""
        raw_map = None
        if getattr(self, "_explicit_site_map", None) is not None:
            raw_map = self._explicit_site_map
        elif (
            (coord := getattr(self, "coordinator", None))
            and isinstance(getattr(coord, "data", None), dict)
            and "site_map" in coord.data
        ):
            raw_map = coord.data["site_map"]
        elif (hass := getattr(self, "hass", None)) and DOMAIN in getattr(
            hass, "data", {}
        ):
            for entry_data in hass.data[DOMAIN].values():
                if isinstance(entry_data, dict) and "site_map" in entry_data:
                    raw_map = entry_data["site_map"]
                    break
        if not raw_map:
            return {}
        normalized: dict[int, str] = {}
        for k, v in raw_map.items():
            try:
                int_k = int(k)
            except (ValueError, TypeError):
                continue
            name = getattr(v, "display_name", v)
            normalized[int_k] = str(name) if name is not None else ""
        return normalized

    @_site_map.setter
    def _site_map(self, value: dict[int, Any] | None) -> None:
        self._explicit_site_map = value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device and location state attributes."""
        try:
            attrs = dict(super().extra_state_attributes or {})
        except (AttributeError, TypeError):
            attrs = {}
        site_gid = getattr(self._device, "site_gid", None)
        attrs["location"] = (
            self._site_map.get(site_gid)
            if site_gid is not None and self._site_map
            else None
        )
        return attrs


class EmporiaChargerEntity(EmporiaLocationEntityMixin, CoordinatorEntity):
    """Emporia Charger Entity."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: pyemvue.PyEmVue,
        device: VueDevice,
        units: str | None = None,
        device_class: str | None = None,
        enabled_default: bool = True,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device: VueDevice = device
        self._device_gid = str(device.device_gid)
        self._vue: pyemvue.PyEmVue = vue
        self._enabled_default: bool = enabled_default
        self._explicit_site_map = site_map

        self._attr_native_unit_of_measurement = units
        self._attr_device_class = device_class
        self._attr_has_entity_name = True
        self._attr_name = None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._device is not None

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether the entity should be enabled when first added to the entity registry."""
        return self._enabled_default

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the device."""
        attrs = dict(super().extra_state_attributes or {})
        data: ChargerDevice | None = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        if data:
            attrs.update(
                {
                    "charging_rate": data.charging_rate,
                    "max_charging_rate": data.max_charging_rate,
                    "status": data.status,
                    "message": data.message,
                    "fault_text": data.fault_text,
                    "icon_name": data.icon,
                    "icon_label": data.icon_label,
                    "icon_detail_text": data.icon_detail_text,
                }
            )
        return attrs

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger."""
        return f"charger.emporia_vue.{self._device_gid}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_gid}-1,2,3")},
            name=self._device.device_name
            or self._device.display_name
            or self._device.model,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
            serial_number=str(self._device.manufacturer_id)
            if self._device.manufacturer_id
            else None,
        )
