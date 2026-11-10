"""Platform for switch integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from pyemvue import PyEmVue
from pyemvue.device import VueDevice

from .charger_entity import (
    EmporiaChargerEntity,
    EmporiaLocationEntityMixin,
    evaluate_charger_switch_is_on,
)
from .const import DOMAIN, VUE_DATA

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    vue: PyEmVue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]
    coordinator: DataUpdateCoordinator | None = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator_device_status"]
    device_information: dict[int, VueDevice] = hass.data[DOMAIN][config_entry.entry_id][
        "device_information"
    ]
    site_map: dict[int, str] = hass.data[DOMAIN][config_entry.entry_id].get(
        "site_map", {}
    )

    if coordinator is None or coordinator.data is None:
        return

    switches = []
    for gid in coordinator.data:
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.channels and all(ch.has_data is False for ch in device.channels):
            continue
        if device.outlet:
            switches.append(
                EmporiaOutletSwitch(coordinator, vue, gid, device, site_map=site_map)
            )
        elif device.ev_charger:
            switches.append(
                EmporiaChargerSwitch(
                    coordinator,
                    vue,
                    device,
                    None,
                    SwitchDeviceClass.OUTLET,
                    site_map=site_map,
                )
            )

    async_add_entities(switches)


class EmporiaOutletSwitch(EmporiaLocationEntityMixin, CoordinatorEntity, SwitchEntity):  # type: ignore
    """Representation of an Emporia Smart Outlet state."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        gid: str,
        device: VueDevice,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._vue = vue
        self._device_gid = gid
        self._device: VueDevice = device
        self._explicit_site_map = site_map
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_device_class = SwitchDeviceClass.OUTLET

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.hass.async_add_executor_job(
            self._vue.update_outlet, self.coordinator.data[self._device_gid], True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.hass.async_add_executor_job(
            self._vue.update_outlet,
            self.coordinator.data[self._device_gid],
            False,
        )
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_gid}-1,2,3")},
            name=self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
            serial_number=str(self._device.manufacturer_id)
            if self._device.manufacturer_id
            else None,
        )

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].outlet_on

    @property
    def unique_id(self) -> str:
        """Unique ID for the switch."""
        return f"switch.emporia_vue.{self._device_gid}"

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        raise NotImplementedError

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        raise NotImplementedError


class EmporiaChargerSwitch(EmporiaChargerEntity, SwitchEntity):  # type: ignore
    """Representation of an Emporia Charger switch state."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        device: VueDevice,
        units: str | None = None,
        device_class: str | None = SwitchDeviceClass.OUTLET,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the charger switch."""
        super().__init__(
            coordinator=coordinator,
            vue=vue,
            device=device,
            units=units,
            device_class=device_class,
            site_map=site_map,
        )
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_icon = "mdi:ev-station"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the charger on."""
        await self._update_switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the charger off."""
        await self._update_switch(False)

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        charger = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        return evaluate_charger_switch_is_on(charger)

    async def _update_switch(self, on: bool) -> None:
        """Update the switch."""
        charger_data = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        dev_id = self.device_id

        try:
            if hasattr(self._vue, "set_evse_state"):
                await self.hass.async_add_executor_job(
                    self._vue.set_evse_state,
                    dev_id,
                    on,
                )
            else:
                await self.hass.async_add_executor_job(
                    self._vue.update_charger,
                    charger_data,
                    on,
                )
        except Exception as err:
            _LOGGER.error(
                "Error updating charger status: %s \nResponse body: %s",
                err,
                getattr(getattr(err, "response", None), "text", ""),
            )
            raise

        await self.coordinator.async_request_refresh()

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the charger on."""
        raise NotImplementedError

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the charger off."""
        raise NotImplementedError
