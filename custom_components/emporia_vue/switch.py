"""Platform for switch integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from pyemvue import PyEmVue
from pyemvue.device import ExcessGenerationLoad, VueDevice
from requests import exceptions

from .charger_entity import (
    EmporiaChargerEntity,
    EmporiaLocationEntityMixin,
    evaluate_charger_switch_is_on,
    resolve_excess_solar_participation,
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

    switches: list[SwitchEntity] = []

    # 1. Controllable hardware device switches (outlets and EV chargers)
    controllable_loads: list[tuple[int, str, VueDevice]] = []
    for gid in coordinator.data:
        if not str(gid).isdigit():
            continue
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.channels and all(ch.has_data is False for ch in device.channels):
            continue
        if device.outlet:
            switches.append(
                EmporiaOutletSwitch(
                    coordinator,
                    vue,
                    str(gid),
                    device,
                    device_information,
                    site_map=site_map,
                )
            )
            load_gid = (
                getattr(device, "load_gid", None)
                or getattr(device.outlet, "load_gid", None)
                or int_gid
            )
            load_name = device.device_name or device.display_name or "Smart plug"
            controllable_loads.append((int(load_gid), load_name, device))
        elif device.ev_charger:
            switches.append(
                EmporiaChargerSwitch(
                    coordinator,
                    vue,
                    device,
                    None,
                    SwitchDeviceClass.OUTLET,
                    device_information,
                    site_map=site_map,
                )
            )
            load_gid = (
                getattr(device, "load_gid", None)
                or getattr(device.ev_charger, "load_gid", None)
                or int_gid
            )
            load_name = device.device_name or device.display_name or "EV Charger"
            controllable_loads.append((int(load_gid), load_name, device))

    # 2. Excess Solar management configuration switches on eligible Vue monitors (Option A)
    excess_monitors = coordinator.data.get("excess_monitors")
    if not excess_monitors:
        for item in coordinator.data.values():
            if getattr(item, "excess_monitors", None):
                excess_monitors = item.excess_monitors
                break

    if excess_monitors and getattr(excess_monitors, "monitors", None):
        for mon in excess_monitors.monitors:
            vue_device = None
            for dev in device_information.values():
                if (
                    (
                        dev.manufacturer_id
                        and dev.manufacturer_id.lower() == mon.device_id.lower()
                    )
                    or str(dev.device_gid) == mon.device_id
                    or getattr(dev, "device_id", None) == mon.device_id
                ):
                    vue_device = dev
                    break
            if not vue_device:
                continue

            for load_gid, load_name, _ in controllable_loads:
                switches.append(
                    EmporiaVueExcessSolarLoadSwitch(
                        coordinator,
                        vue,
                        vue_device,
                        mon.device_id,
                        load_gid,
                        load_name,
                        device_information,
                        site_map=site_map,
                    )
                )

    async_add_entities(switches)


class EmporiaOutletSwitch(
    EmporiaLocationEntityMixin, CoordinatorEntity, SwitchEntity
):  # type: ignore
    """Representation of an Emporia Smart Outlet state."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        gid: str,
        device: VueDevice,
        device_information: dict[int, VueDevice] | None = None,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._vue = vue
        self._device_gid = gid
        self._device: VueDevice = device
        self._device_information = device_information or {}
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

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].outlet_on

    @property
    def unique_id(self) -> str:
        """Unique ID for the switch."""
        return f"switch.emporia_vue.{self._device_gid}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return outlet extra state attributes."""
        attrs: dict[str, Any] = dict(super().extra_state_attributes or {})
        excess_solar = (
            self.coordinator.data.get("excess_solar_by_device_id")
            if self.coordinator and self.coordinator.data
            else None
        )
        if not excess_solar:
            outlet_data = (
                self.coordinator.data.get(self._device_gid)
                if self.coordinator and self.coordinator.data
                else None
            )
            excess_solar = getattr(outlet_data, "excess_solar_by_device_id", {})
        excess_solar = excess_solar or {}

        load_gid = (
            getattr(self._device, "load_gid", None)
            or getattr(getattr(self._device, "outlet", None), "load_gid", None)
            or int(self._device_gid)
        )
        enabled = False
        managing_vue = None
        for vue_id, eg in excess_solar.items():
            if eg and hasattr(eg, "loads"):
                for ld in eg.loads:
                    if getattr(ld, "load_gid", None) == load_gid and getattr(
                        ld, "enabled", False
                    ):
                        enabled = True
                        for dev in self._device_information.values():
                            if (
                                dev.manufacturer_id
                                and dev.manufacturer_id.lower() == vue_id.lower()
                            ) or str(dev.device_gid) == vue_id:
                                managing_vue = (
                                    dev.device_name or dev.display_name or vue_id
                                )
                                break
                        if not managing_vue:
                            managing_vue = vue_id
                        break
            if enabled:
                break

        if managing_vue:
            attrs["excess_solar_managing_vue"] = managing_vue
        return attrs

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
        device_information: dict[int, VueDevice] | None = None,
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
        self._device_information = device_information or {}
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return charger extra state attributes."""
        attrs = dict(super().extra_state_attributes or {})
        charger = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        _, managing_vue = resolve_excess_solar_participation(
            self.coordinator.data,
            self._device_information,
            self.target_load_gid,
            int(self._device_gid),
            fallback_holder=charger,
        )
        if managing_vue:
            attrs["excess_solar_managing_vue"] = managing_vue
        return attrs

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


class EmporiaVueExcessSolarLoadSwitch(
    EmporiaLocationEntityMixin, CoordinatorEntity, SwitchEntity
):  # type: ignore
    """Representation of an Excess Solar load participation switch on an Energy Monitor."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        vue_device: VueDevice,
        vue_id: str,
        load_gid: int,
        load_name: str,
        device_information: dict[int, VueDevice],
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the excess solar load switch."""
        super().__init__(coordinator)
        self._vue = vue
        self._vue_device = vue_device
        self._device = vue_device
        self._explicit_site_map = site_map
        self._vue_id = vue_id
        self._load_gid = load_gid
        self._load_name = load_name
        self._device_information = device_information
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_translation_key = "excess_solar_load"
        self._attr_translation_placeholders = {"load": load_name}
        self._attr_name = f"Excess solar {load_name}"
        self._attr_icon = "mdi:solar-power"
        self._attr_has_entity_name = True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return excess solar load switch extra state attributes."""
        return dict(super().extra_state_attributes or {})

    @property
    def unique_id(self) -> str:
        """Unique ID for the switch entity."""
        return (
            f"emporia_vue.excess_solar_{self._vue_device.device_gid}_{self._load_gid}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information attaching this entity to the Vue monitor."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._vue_device.device_gid}-1,2,3")},
            name=self._vue_device.device_name
            or self._vue_device.display_name
            or self._vue_device.model,
            model=self._vue_device.model,
            sw_version=self._vue_device.firmware,
            manufacturer="Emporia",
            serial_number=str(self._vue_device.manufacturer_id)
            if self._vue_device.manufacturer_id
            else None,
        )

    def _get_excess_solar_map(self) -> dict[str, Any]:
        """Return the dictionary mapping monitor IDs to excess solar configuration."""
        if not self.coordinator or not self.coordinator.data:
            return {}
        excess_solar = self.coordinator.data.get("excess_solar_by_device_id")
        if not excess_solar:
            for item in self.coordinator.data.values():
                if getattr(item, "excess_solar_by_device_id", None):
                    excess_solar = item.excess_solar_by_device_id
                    break
        return excess_solar or {}

    def _resolve_vue_name(self, vue_id: str) -> str:
        """Resolve a monitor ID to human-friendly display name."""
        for dev in self._device_information.values():
            if (
                (dev.manufacturer_id and dev.manufacturer_id.lower() == vue_id.lower())
                or str(dev.device_gid) == vue_id
                or getattr(dev, "device_id", None) == vue_id
            ):
                return dev.device_name or dev.display_name or vue_id
        return vue_id

    @property
    def is_on(self) -> bool:
        """Return True if this load is enabled for excess solar on this Vue."""
        excess_solar = self._get_excess_solar_map()
        em_eg = excess_solar.get(self._vue_id)
        if em_eg and hasattr(em_eg, "loads"):
            for load in em_eg.loads:
                if getattr(load, "load_gid", None) == self._load_gid:
                    return bool(getattr(load, "enabled", False))
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable excess solar for this load on this Vue."""
        excess_solar = self._get_excess_solar_map()

        # Approach 2 Exclusivity Guard:
        # Check if this load is already enabled on ANY other Vue monitor
        for other_id, other_eg in excess_solar.items():
            if other_id != self._vue_id and other_eg and hasattr(other_eg, "loads"):
                for load in other_eg.loads:
                    if getattr(load, "load_gid", None) == self._load_gid and getattr(
                        load, "enabled", False
                    ):
                        other_vue_name = self._resolve_vue_name(other_id)
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="load_already_managed",
                            translation_placeholders={
                                "load": self._load_name,
                                "vue": other_vue_name,
                            },
                        )

        target_eg = excess_solar.get(self._vue_id)
        loads: list[ExcessGenerationLoad] = []
        solar_window = None
        sunrise = None
        sunset = None
        if target_eg:
            solar_window = getattr(target_eg, "solar_window", None)
            sunrise = getattr(target_eg, "sunrise", None)
            sunset = getattr(target_eg, "sunset", None)
            if hasattr(target_eg, "loads"):
                loads = [
                    ld
                    for ld in target_eg.loads
                    if getattr(ld, "load_gid", None) != self._load_gid
                ]
        loads.append(ExcessGenerationLoad(load_gid=self._load_gid, enabled=True))

        try:
            await self.hass.async_add_executor_job(
                self._vue.set_excess_generation,
                self._vue_id,
                loads,
                solar_window,
                sunrise,
                sunset,
            )
        except exceptions.HTTPError as err:
            if (
                err.response is not None
                and "LOAD_ALREADY_CONTROLLED" in err.response.text
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="load_already_managed",
                    translation_placeholders={
                        "load": self._load_name,
                        "vue": "another energy monitor",
                    },
                ) from err
            _LOGGER.error(
                "Error enabling excess solar for %s on %s: %s",
                self._load_name,
                self._vue_id,
                err,
            )
            raise

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable excess solar for this load on this Vue."""
        excess_solar = self._get_excess_solar_map()
        target_eg = excess_solar.get(self._vue_id)
        loads: list[ExcessGenerationLoad] = []
        solar_window = None
        sunrise = None
        sunset = None
        if target_eg:
            solar_window = getattr(target_eg, "solar_window", None)
            sunrise = getattr(target_eg, "sunrise", None)
            sunset = getattr(target_eg, "sunset", None)
            if hasattr(target_eg, "loads"):
                loads = [
                    ld
                    for ld in target_eg.loads
                    if getattr(ld, "load_gid", None) != self._load_gid
                ]
        loads.append(ExcessGenerationLoad(load_gid=self._load_gid, enabled=False))

        try:
            await self.hass.async_add_executor_job(
                self._vue.set_excess_generation,
                self._vue_id,
                loads,
                solar_window,
                sunrise,
                sunset,
            )
        except exceptions.HTTPError as err:
            _LOGGER.error(
                "Error disabling excess solar for %s on %s: %s",
                self._load_name,
                self._vue_id,
                err,
            )
            raise

        await self.coordinator.async_request_refresh()

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        raise NotImplementedError

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        raise NotImplementedError
