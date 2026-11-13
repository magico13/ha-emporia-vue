"""Platform for button integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyemvue import PyEmVue
from pyemvue.device import VueDevice
from requests import exceptions

from .charger_entity import (
    EmporiaChargerEntity,
    evaluate_full_power_override_available,
)
from .const import DOMAIN, VUE_DATA

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    vue: PyEmVue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]
    coordinator: DataUpdateCoordinator | None = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator_device_status"]
    device_information: dict[int, VueDevice] = hass.data[DOMAIN][config_entry.entry_id][
        "device_information"
    ]
    site_map: dict[int, Any] | None = hass.data[DOMAIN][config_entry.entry_id].get(
        "site_map"
    )

    if coordinator is None or coordinator.data is None:
        return

    buttons: list[ButtonEntity] = []
    for gid in coordinator.data:
        if not str(gid).isdigit():
            continue
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.ev_charger:
            buttons.append(
                EmporiaChargerFullPowerOverrideButton(
                    coordinator, vue, device, site_map=site_map
                )
            )

    async_add_entities(buttons)


class EmporiaChargerFullPowerOverrideButton(EmporiaChargerEntity, ButtonEntity):  # type: ignore
    """Button entity to immediately override solar/scheduled throttles and charge at full power."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        device: VueDevice,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the full power override button."""
        super().__init__(coordinator, vue, device, site_map=site_map)
        self._attr_translation_key = "charge_at_full_power"
        self._attr_name = "Charge at full power"
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def unique_id(self) -> str:
        """Unique ID for the button entity."""
        return f"emporia_vue.charge_full_power_{self._device_gid}"

    @property
    def available(self) -> bool:
        """Return True if button is available to be pressed."""
        if not super().available:
            return False
        charger = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        return evaluate_full_power_override_available(charger)

    async def async_press(self) -> None:
        """Handle the button press."""
        dev_id = self.device_id
        try:
            if hasattr(self._vue, "post_evse_control"):
                await self.hass.async_add_executor_job(
                    self._vue.post_evse_control,
                    dev_id,
                    "CHARGE_AT_FULL_POWER",
                )
            else:
                _LOGGER.warning(
                    "post_evse_control not available in current PyEmVue library"
                )
        except exceptions.HTTPError as err:
            _LOGGER.error(
                "Error executing charge at full power override: %s \nResponse body: %s",
                err,
                err.response.text if err.response is not None else "",
            )
            raise

        await self.coordinator.async_request_refresh()
