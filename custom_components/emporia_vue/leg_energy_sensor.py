from datetime import datetime
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import CoordinatorEntity

W_TO_KWH_PER_SEC = 1.0 / 3_600_000.0  # watts * seconds -> kWh


class LegEnergySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Total-increasing kWh sensor for a single mains leg (L1/L2/L3)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, device_id: str, leg_key: str, name: str, unique_id: str, device_info: dict):
        super().__init__(coordinator)
        self._device_id = device_id
        self._leg_key = leg_key  # e.g. "l1", "l2", "l3"
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._attr_native_value = 0.0
        self._last_dt: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known kWh total and init timestamp."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                self._attr_native_value = 0.0
        self._last_dt = dt_util.utcnow()

    def _get_leg_power_w(self) -> float | None:
        """Pull the W value for this leg from coordinator.data."""
        device = self.coordinator.data.get(self._device_id)
        if not device:
            return None
        return device.get("legs", {}).get(self._leg_key)

    def _handle_coordinator_update(self) -> None:
        now = dt_util.utcnow()
        if self._last_dt is None:
            self._last_dt = now
            return

        power_w = self._get_leg_power_w()
        if power_w is not None:
            delta_s = (now - self._last_dt).total_seconds()
            if delta_s > 0:
                self._attr_native_value += max(power_w *
                                               delta_s * W_TO_KWH_PER_SEC, 0)

        self._last_dt = now
        self.async_write_ha_state()
