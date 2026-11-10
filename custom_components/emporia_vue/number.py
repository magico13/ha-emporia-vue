"""Platform for number integration."""

import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyemvue import PyEmVue
from pyemvue.device import VueDevice
from requests import exceptions

from .charger_entity import EmporiaChargerEntity
from .const import DOMAIN, VUE_DATA

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
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

    entities = []
    for gid in coordinator.data:
        if not str(gid).isdigit():
            continue
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.ev_charger:
            entities.append(
                EmporiaChargerCurrentNumber(coordinator, vue, device, site_map=site_map)
            )

    async_add_entities(entities)


class EmporiaChargerCurrentNumber(EmporiaChargerEntity, NumberEntity):  # type: ignore
    """Representation of the Emporia EV Charger current limit."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        device: VueDevice,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(
            coordinator,
            vue,
            device,
            UnitOfElectricCurrent.AMPERE,
            NumberDeviceClass.CURRENT,
            site_map=site_map,
        )
        self._attr_native_min_value = 6.0
        self._attr_native_max_value = float(device.ev_charger.max_charging_rate)
        self._attr_native_step = 1.0
        self._attr_translation_key = "charge_current_limit"
        self._attr_icon = "mdi:current-ac"
        self._attr_name = "Current Limit"

    @property
    def unique_id(self) -> str:
        """Unique ID for the number entity."""
        return f"emporia_vue.charger_current_{self._device_gid}"

    @property
    def native_value(self) -> float | None:
        """Return the current charging rate."""
        data = self.coordinator.data.get(self._device_gid)
        if data:
            return float(data.charging_rate)
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the charger current limit."""
        current = int(value)
        current = max(6, min(current, int(self._attr_native_max_value)))
        charger_data = self.coordinator.data.get(self._device_gid)
        dev_id = self.device_id
        try:
            if hasattr(self._vue, "set_evse_settings"):
                await self.hass.async_add_executor_job(
                    self._vue.set_evse_settings,
                    dev_id,
                    current,
                )
            else:
                await self.hass.async_add_executor_job(
                    self._vue.update_charger,
                    charger_data,
                    charger_data.charger_on if charger_data else True,
                    current,
                )
        except exceptions.HTTPError as err:
            _LOGGER.error(
                "Error updating charger current: %s \nResponse body: %s",
                err,
                err.response.text if err.response is not None else "",
            )
            raise
        await self.coordinator.async_request_refresh()
