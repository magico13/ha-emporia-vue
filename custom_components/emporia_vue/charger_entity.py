"""Emporia Charger Entity and Deterministic EVSE Evaluators."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from pyemvue import pyemvue
from pyemvue.device import ChargerDevice, VueDevice

from .const import DOMAIN

OPERATING_STATE_OPTIONS: list[str] = [
    "fault",
    "offline",
    "off",
    "armed",
    "ready",
    "ready_excess_solar",
    "offering_charge",
    "waiting_for_solar",
    "idle",
    "vehicle_ended_charge",
    "charging",
    "charging_excess_solar",
    "charging_peak_demand",
    "charging_power_smart",
    "charging_load_sharing",
    "charging_full_power_override",
    "paused",
    "paused_manual",
    "paused_excess_solar",
    "paused_peak_demand",
    "paused_schedule",
    "paused_time_of_use",
    "paused_utility",
]

OPERATING_STATE_TO_SUBSTATUS: dict[str, str] = {
    "fault": "Fault",
    "off": "Off",
    "armed": "Armed",
    "ready": "Ready",
    "ready_excess_solar": "Ready (excess solar)",
    "offering_charge": "Offering charge",
    "waiting_for_solar": "Waiting for solar",
    "idle": "Idle",
    "vehicle_ended_charge": "Vehicle ended charge",
    "charging": "Standard",
    "charging_excess_solar": "Excess solar",
    "charging_peak_demand": "Peak demand",
    "charging_power_smart": "PowerSmart",
    "charging_load_sharing": "Load sharing",
    "charging_full_power_override": "Full power override",
    "paused": "Paused",
    "paused_manual": "Paused manually",
    "paused_excess_solar": "Paused (excess solar)",
    "paused_peak_demand": "Paused (peak demand)",
    "paused_schedule": "Paused (schedule)",
    "paused_time_of_use": "Paused (time of use)",
    "paused_utility": "Paused (utility)",
}

OPERATING_STATE_ICONS: dict[str, str] = {
    "fault": "mdi:alert-circle",
    "offline": "mdi:lan-disconnect",
    "off": "mdi:ev-station",
    "armed": "mdi:flash-outline",
    "ready": "mdi:car-connected",
    "ready_excess_solar": "mdi:solar-power",
    "offering_charge": "mdi:ev-plug-type1",
    "waiting_for_solar": "mdi:weather-sunny-alert",
    "idle": "mdi:sleep",
    "vehicle_ended_charge": "mdi:battery-check",
    "charging": "mdi:battery-charging",
    "charging_excess_solar": "mdi:solar-power-variant",
    "charging_peak_demand": "mdi:chart-bell-curve-cumulative",
    "charging_power_smart": "mdi:shield-lightning-outline",
    "charging_load_sharing": "mdi:call-split",
    "charging_full_power_override": "mdi:lightning-bolt",
    "paused": "mdi:pause-circle-outline",
    "paused_manual": "mdi:pause-circle",
    "paused_excess_solar": "mdi:weather-partly-cloudy",
    "paused_peak_demand": "mdi:transmission-tower-off",
    "paused_schedule": "mdi:calendar-clock",
    "paused_time_of_use": "mdi:clock-alert-outline",
    "paused_utility": "mdi:transmission-tower-import",
}


def _clean_upper_str(value: Any) -> str:
    """Return stripped uppercase string if value is str, else empty string."""
    return value.strip().upper() if isinstance(value, str) else ""


def resolve_effective_charger_status(charger: ChargerDevice | None) -> str:
    """Return authoritative uppercase charger status from evse_control or charger.status."""
    if charger is None:
        return ""
    evse_ctrl = getattr(charger, "evse_control", None)
    ctrl_status = _clean_upper_str(getattr(evse_ctrl, "charger_status", None))
    if ctrl_status and ctrl_status != "UNKNOWN_VALUE":
        return ctrl_status
    return _clean_upper_str(getattr(charger, "status", None))


def is_pro_control_locked(charger: ChargerDevice | None) -> bool:
    """Return True iff charger is locked under Emporia Invite / Partner Pro Control."""
    if charger is None:
        return False
    code = getattr(charger, "pro_control_code", None)
    return isinstance(code, str) and bool(code.strip())


def has_charger_fault(charger: ChargerDevice | None) -> bool:
    """Return True iff EVSE has an active hardware fault condition (IEC State F)."""
    if charger is None:
        return False
    evse_ctrl = getattr(charger, "evse_control", None)
    fault_code = (
        getattr(evse_ctrl, "fault_code", None) if evse_ctrl is not None else None
    )
    if isinstance(fault_code, int) or (
        isinstance(fault_code, str) and bool(fault_code.strip())
    ):
        return True
    fault_text = getattr(charger, "fault_text", None)
    if isinstance(fault_text, str) and bool(fault_text.strip()):
        return True
    status_upper = resolve_effective_charger_status(charger)
    if status_upper == "FAULTING" or "FAULT" in status_upper or "ERROR" in status_upper:
        return True
    message_upper = _clean_upper_str(getattr(charger, "message", None))
    return "FAULT" in message_upper or "ERROR" in message_upper


def is_charger_offline(charger: ChargerDevice | None) -> bool:
    """Return True iff EVSE is disconnected from Emporia cloud telemetry."""
    if charger is None:
        return True
    status_upper = resolve_effective_charger_status(charger)
    if status_upper in ("OFFLINE", "DEVICENOTCONNECTED"):
        return True
    evse_ctrl = getattr(charger, "evse_control", None)
    subtext_upper = _clean_upper_str(getattr(evse_ctrl, "subtext_type", None))
    if subtext_upper == "OFFLINE":
        return True
    message_upper = _clean_upper_str(getattr(charger, "message", None))
    return status_upper == "STANDBY" and message_upper == "OFFLINE"


def resolve_excess_solar_participation(
    coordinator_data: dict[str, Any] | None,
    device_information: dict[int, VueDevice] | None,
    target_load_gid: int,
    device_gid: int,
    fallback_holder: Any = None,
) -> tuple[bool, str | None]:
    """Exhaustively evaluate all monitors for active Excess Solar enrollment.

    Never exits early on a disabled monitor record.
    Returns (excess_solar_enabled, managing_vue_display_name).
    """
    excess_solar: dict[str, Any] = {}
    if coordinator_data:
        raw_map = coordinator_data.get("excess_solar_by_device_id")
        if isinstance(raw_map, dict) and raw_map:
            excess_solar = raw_map
    if not excess_solar and fallback_holder is not None:
        raw_fallback = getattr(fallback_holder, "excess_solar_by_device_id", None)
        if isinstance(raw_fallback, dict):
            excess_solar = raw_fallback

    info_map = device_information or {}
    candidate_gids = {target_load_gid, device_gid}

    for vue_id, eg in excess_solar.items():
        loads = getattr(eg, "loads", None)
        if not loads:
            continue
        for ld in loads:
            if getattr(ld, "load_gid", None) in candidate_gids and bool(
                getattr(ld, "enabled", False)
            ):
                managing_vue: str | None = None
                for dev in info_map.values():
                    manuf_id = getattr(dev, "manufacturer_id", None)
                    if (
                        isinstance(manuf_id, str)
                        and manuf_id.lower() == str(vue_id).lower()
                    ) or str(dev.device_gid) == str(vue_id):
                        managing_vue = (
                            dev.device_name or dev.display_name or str(vue_id)
                        )
                        break
                return True, (managing_vue or str(vue_id))

    return False, None


def evaluate_charger_switch_available(charger: ChargerDevice | None) -> bool:
    """Return True iff switch.<evse> can accept TURN_ON / TURN_OFF commands."""
    if charger is None:
        return False
    return not is_charger_offline(charger)


def evaluate_charger_switch_is_on(charger: ChargerDevice | None) -> bool:
    """Return True iff EVSE contactor is armed, offering current, or actively charging."""
    if charger is None:
        return False
    if has_charger_fault(charger) or is_charger_offline(charger):
        return False

    status_upper = resolve_effective_charger_status(charger)
    if status_upper in ("CHARGING", "CONNECTED_ON", "DISCONNECTED_ON"):
        return True
    if status_upper in ("CONNECTED_OFF", "DISCONNECTED_OFF"):
        return False

    evse_ctrl = getattr(charger, "evse_control", None)
    if evse_ctrl is not None:
        action = _clean_upper_str(getattr(evse_ctrl, "button_action", None))
        if action in ("TURN_OFF", "STOP", "CANCEL", "MANAGE"):
            return True
        if action in ("TURN_ON", "CHARGE_NOW", "RESUME", "TROUBLESHOOT"):
            return False

    return bool(getattr(charger, "charger_on", False))


def evaluate_full_power_override_available(charger: ChargerDevice | None) -> bool:
    """Return True iff CHARGE_AT_FULL_POWER is an executable state transition."""
    if not evaluate_charger_switch_available(charger):
        return False
    evse_ctrl = getattr(charger, "evse_control", None)
    if evse_ctrl is None:
        return False

    behavior = _clean_upper_str(getattr(evse_ctrl, "button_behavior", None))
    if behavior in (
        "OVERRIDE_EXCESS_SOLAR",
        "OVERRIDE_PEAK_DEMAND_CHARGE_OR_PAUSE",
    ):
        return True
    if behavior:
        return False

    status_upper = resolve_effective_charger_status(charger)
    rate_ctrl = _clean_upper_str(getattr(evse_ctrl, "charging_rate_controller", None))
    return status_upper in ("CHARGING", "CONNECTED_ON") and rate_ctrl in (
        "EXCESS_SOLAR",
        "PEAK_DEMAND",
    )


def evaluate_iec_charger_status(charger: ChargerDevice | None) -> tuple[str, str]:
    """Return backward-compatible (state_label, iec_61851_code) tuple."""
    if charger is None:
        return "Disconnected", "A"
    if has_charger_fault(charger):
        return "Error", "F"
    status_upper = resolve_effective_charger_status(charger)
    if status_upper == "CHARGING":
        return "Charging", "C"
    if is_charger_offline(charger) or not status_upper:
        return "Disconnected", "A"
    if status_upper in ("DISCONNECTED_OFF", "DISCONNECTED_ON", "UNKNOWN_VALUE"):
        return "Disconnected", "A"
    message_lower = (
        charger.message.strip().lower()
        if isinstance(getattr(charger, "message", None), str)
        else ""
    )
    if status_upper == "STANDBY" and message_lower in (
        "ready",
        "off",
        "self test",
        "please wait",
        "offline",
    ):
        return "Disconnected", "A"
    return "Connected", "B"


def evaluate_operating_state(
    charger: ChargerDevice | None,
    excess_solar_enabled: bool,
) -> str:
    """Map EVSE state deterministically to one of the 23 OPERATING_STATE_OPTIONS."""
    if charger is None:
        return "offline"
    if has_charger_fault(charger):
        return "fault"
    if is_charger_offline(charger):
        return "offline"

    evse_ctrl = getattr(charger, "evse_control", None)
    status_upper = resolve_effective_charger_status(charger)
    subtext = _clean_upper_str(getattr(evse_ctrl, "subtext_type", None))
    rate_ctrl = _clean_upper_str(getattr(evse_ctrl, "charging_rate_controller", None))
    session_active = bool(getattr(evse_ctrl, "session_active", False) is True)

    if status_upper == "DISCONNECTED_OFF":
        return "off"
    if status_upper == "DISCONNECTED_ON":
        return "armed"

    if status_upper == "CONNECTED_ON":
        if subtext == "VEHICLE_STOPPED":
            return "idle"
        if subtext == "WAITING_FOR_SOLAR":
            return "waiting_for_solar"
        return "offering_charge"

    if status_upper == "CHARGING":
        if rate_ctrl == "EXCESS_SOLAR":
            return "charging_excess_solar"
        if rate_ctrl == "PEAK_DEMAND":
            return "charging_peak_demand"
        if rate_ctrl == "POWER_SMART":
            return "charging_power_smart"
        if rate_ctrl == "LOAD_SHARING":
            return "charging_load_sharing"
        if subtext in ("EXCESS_SOLAR_PAUSED", "PEAK_DEMAND_PAUSED"):
            return "charging_full_power_override"
        return "charging"

    if status_upper == "CONNECTED_OFF":
        if subtext == "VEHICLE_STOPPED":
            return "vehicle_ended_charge"
        if subtext == "MANUALLY_STOPPED":
            return "paused_manual"
        if subtext in ("PEAK_DEMAND_STOPPED", "PEAK_DEMAND_PAUSED"):
            return "paused_peak_demand"
        if subtext == "SCHEDULE_STOPPED":
            return "paused_schedule"
        if subtext == "TOU_STOPPED":
            return "paused_time_of_use"
        if subtext == "UTILITY_STOPPED":
            return "paused_utility"
        if subtext == "EXCESS_SOLAR_PAUSED":
            params = getattr(evse_ctrl, "subtext_parameters", None) or []
            return "paused_excess_solar" if params else "paused_manual"
        if session_active:
            if subtext == "WAITING_FOR_SOLAR" or excess_solar_enabled:
                return "paused_excess_solar"
            return "paused"
        if subtext == "WAITING_FOR_SOLAR" or excess_solar_enabled:
            return "ready_excess_solar"
        return "ready"

    # Legacy API fallback ("Standby", "Ready", or empty string)
    message_lower = (
        charger.message.strip().lower()
        if isinstance(getattr(charger, "message", None), str)
        else ""
    )
    charger_on = bool(getattr(charger, "charger_on", False))
    if status_upper == "STANDBY":
        if message_lower == "ev is not accepting charge":
            return "idle"
        if message_lower == "charging halted":
            return "paused"
        if message_lower == "connected to ev":
            return "offering_charge" if charger_on else "ready"
        if message_lower in ("ready", "self test", "please wait"):
            return "armed" if charger_on else "off"
        return "off"
    if status_upper == "READY":
        return "ready_excess_solar" if excess_solar_enabled else "ready"
    return "armed" if charger_on else "off"


def evaluate_charger_substatus(
    charger: ChargerDevice | None,
    excess_solar_enabled: bool,
) -> str | None:
    """Map EVSE operational state deterministically to canonical substatus string."""
    if charger is None or is_charger_offline(charger):
        return None
    op_state = evaluate_operating_state(charger, excess_solar_enabled)
    return OPERATING_STATE_TO_SUBSTATUS.get(op_state, op_state)


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
        if self._device is None:
            return False
        charger = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        return evaluate_charger_switch_available(charger)

    @property
    def device_id(self) -> str:
        """Return the canonical device ID (e.g. manufacturer_id) for API calls."""
        charger_data = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        dev_id = (
            getattr(charger_data, "device_id", None)
            or getattr(self._device, "manufacturer_id", None)
            or getattr(self._device, "device_id", None)
        )
        return str(dev_id or self._device_gid)

    @property
    def target_load_gid(self) -> int:
        """Return the controllable load GID for this charger."""
        charger_data = (
            self.coordinator.data.get(self._device_gid)
            if self.coordinator and self.coordinator.data
            else None
        )
        ev_charger = getattr(self._device, "ev_charger", None)
        gid = (
            getattr(self._device, "load_gid", None)
            or getattr(charger_data, "load_gid", None)
            or getattr(ev_charger, "load_gid", None)
            or self._device_gid
        )
        try:
            return int(gid)
        except (ValueError, TypeError):
            return int(self._device_gid)

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
            status_upper = resolve_effective_charger_status(data)
            if status_upper == "CHARGING":
                legacy_status = "Charging"
            elif (
                status_upper in ("OFFLINE", "DEVICENOTCONNECTED")
                or is_charger_offline(data)
            ):
                legacy_status = "DeviceNotConnected"
            else:
                legacy_status = "Standby"

            attrs.update(
                {
                    "charging_rate": data.charging_rate,
                    "max_charging_rate": data.max_charging_rate,
                    "status": legacy_status,
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
