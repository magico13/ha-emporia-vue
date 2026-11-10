"""Platform for sensor integration."""

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyemvue.device import ChargerDevice, VueDevice, VueDeviceChannel
from pyemvue.enums import Scale

from . import determine_circuit_model
from .charger_entity import (
    EmporiaLocationEntityMixin,
    evaluate_charger_substatus,
    evaluate_iec_charger_status,
    is_charger_offline,
    resolve_effective_charger_status,
    resolve_excess_solar_participation,
)
from .const import DOMAIN
from .resilience import TolerantUpdateMethod

_LOGGER: logging.Logger = logging.getLogger(__name__)

SPLIT_PHASE_MAINS_CHANNELS = ("Mains_A", "Mains_B", "Mains_C")


# def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator_1min = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1min"]
    coordinator_1mon = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1mon"]
    coordinator_day_sensor = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_day_sensor"
    ]

    _LOGGER.info(hass.data[DOMAIN][config_entry.entry_id])

    site_map: dict[int, str] = hass.data[DOMAIN][config_entry.entry_id].get(
        "site_map", {}
    )

    def _is_channel_active(coordinator, identifier: str) -> bool:
        if not coordinator or not coordinator.data:
            return False
        channel_data = coordinator.data.get(identifier)
        if not channel_data:
            return False
        device = channel_data.get("info")
        channel_num = channel_data.get("channel_num")
        if device and device.channels:
            for ch in device.channels:
                if ch.channel_num == channel_num:
                    return ch.has_data is not False
        return True

    if coordinator_1min:
        power_sensors: list[SensorEntity] = []
        voltage_sensors: list[SensorEntity] = []
        for identifier in coordinator_1min.data:
            if not _is_channel_active(coordinator_1min, identifier):
                continue
            channel_data = coordinator_1min.data[identifier]
            channel_num = channel_data.get("channel_num")
            if channel_num not in SPLIT_PHASE_MAINS_CHANNELS:
                power_sensors.append(
                    CurrentVuePowerSensor(
                        coordinator_1min, identifier, site_map=site_map
                    )
                )
            elif channel_data.get("voltage") is not None:
                voltage_sensors.append(
                    CurrentVueVoltageSensor(
                        coordinator_1min, identifier, site_map=site_map
                    )
                )

        async_add_entities(power_sensors)
        if voltage_sensors:
            async_add_entities(voltage_sensors)

    if coordinator_1mon:
        async_add_entities(
            CurrentVuePowerSensor(coordinator_1mon, identifier, site_map=site_map)
            for identifier in coordinator_1mon.data
            if _is_channel_active(coordinator_1mon, identifier)
            and coordinator_1mon.data[identifier].get("channel_num")
            not in SPLIT_PHASE_MAINS_CHANNELS
        )

    if coordinator_day_sensor:
        async_add_entities(
            CurrentVuePowerSensor(
                coordinator_day_sensor, identifier, site_map=site_map
            )
            for identifier in coordinator_day_sensor.data
            if _is_channel_active(coordinator_day_sensor, identifier)
            and coordinator_day_sensor.data[identifier].get("channel_num")
            not in SPLIT_PHASE_MAINS_CHANNELS
        )

    retry_update_methods: dict[str, TolerantUpdateMethod] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["retry_update_methods"]
    if retry_update_methods:
        async_add_entities(
            [EmporiaApiRetrySensor(retry_update_methods, config_entry.entry_id)]
        )
        if "minute" in retry_update_methods:
            async_add_entities(
                [EmporiaApiLatencySensor(retry_update_methods, config_entry.entry_id)]
            )

    # Add charger status sensors
    coordinator_device_status = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_device_status"
    ]
    device_information: dict[int, VueDevice] = hass.data[DOMAIN][config_entry.entry_id][
        "device_information"
    ]
    if coordinator_device_status and coordinator_device_status.data:
        charger_sensors: list[SensorEntity] = []
        for gid in coordinator_device_status.data:
            if not str(gid).isdigit():
                continue
            int_gid = int(gid)
            if int_gid in device_information and device_information[int_gid].ev_charger:
                dev = device_information[int_gid]
                charger_sensors.append(
                    EmporiaChargerStatusSensor(
                        coordinator_device_status,
                        dev,
                        device_information,
                        site_map=site_map,
                    )
                )
        if charger_sensors:
            async_add_entities(charger_sensors)


class CurrentVuePowerSensor(
    EmporiaLocationEntityMixin, CoordinatorEntity, SensorEntity
):  # type: ignore
    """Representation of a Vue Sensor's current power."""

    def __init__(
        self,
        coordinator,
        identifier: str,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._explicit_site_map = site_map
        self._scale: str = coordinator.data[identifier]["scale"]
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]
        final_channel: VueDeviceChannel | None = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    final_channel = channel
                    break
        if final_channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        self._channel: VueDeviceChannel = final_channel
        self._iskwh = self.scale_is_energy()

        self._attr_has_entity_name = True
        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 3
            self._attr_name = f"Energy {self.scale_readable()}"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            if channel_num in SPLIT_PHASE_MAINS_CHANNELS:
                leg = channel_num.replace("Mains_", "")
                self._attr_name = f"Mains {leg} power"
            else:
                self._attr_name = f"Power {self.scale_readable()}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        monitor_name = (
            self._device.device_name
            or self._device.display_name
            or self._device.model
            or f"Vue Device {self._device.device_gid}"
        )
        if self._channel.name:
            device_name = self._channel.name
        else:
            # An unnamed *numbered* channel is a CT that has not been configured
            # in the Emporia app. Falling back to the monitor's name gives every
            # such channel an identical name, so a Vue with spare channels shows
            # up as a pile of same-named devices (#379, #328).
            # Aggregate channels ("1,2,3", MainsFromGrid, Balance, ...) legitimately
            # represent the monitor itself, so those keep the monitor's name.
            if self._channel.channel_num.isdigit():
                device_name = f"{monitor_name} circuit {self._channel.channel_num}"
            elif self._channel.channel_num in ("1,2,3", *SPLIT_PHASE_MAINS_CHANNELS):
                device_name = monitor_name
            else:
                device_name = f"{monitor_name} {self._channel.channel_num}"

        if self._channel.channel_num in ("1,2,3", *SPLIT_PHASE_MAINS_CHANNELS):
            return DeviceInfo(
                identifiers={(DOMAIN, f"{self._device.device_gid}-1,2,3")},
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

        dev_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{self._device.device_gid}-{self._channel.channel_num}")
            },
            name=device_name,
            model=determine_circuit_model(self._channel, self._device),
            model_id=None,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )
        if not self._channel.name and self._channel.channel_num.isdigit():
            dev_info["translation_key"] = "circuit"
            dev_info["translation_placeholders"] = {
                "device_name": monitor_name,
                "channel_number": str(self._channel.channel_num),
            }
        return dev_info

    @property
    def last_reset(self) -> datetime | None:
        """Reset time of the daily/monthly sensor. Midnight local time."""
        if self._id in self.coordinator.data:
            return self.coordinator.data[self._id]["reset"]
        return None

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            usage = self.coordinator.data[self._id]["usage"]
            return self.scale_usage(usage) if usage is not None else None
        return None

    @property
    def unique_id(self) -> str:
        """Return the Unique ID for the sensor."""
        device_gid = self._channel.device_gid or self._device.device_gid
        if self._scale == Scale.MINUTE.value:
            return (
                f"sensor.emporia_vue.instant.{device_gid}-{self._channel.channel_num}"
            )
        return (
            f"sensor.emporia_vue.{self._scale}.{device_gid}-{self._channel.channel_num}"
        )

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = 60 * 1000 * usage  # convert from kwh to w rate
        elif self._scale == Scale.SECOND.value:
            usage = 3600 * 1000 * usage  # convert to rate
        elif self._scale == Scale.MINUTES_15.value:
            usage = (
                4 * 1000 * usage
            )  # this might never be used but for safety, convert to rate
        return usage

    def scale_is_energy(self):
        """Return True if the scale is an energy unit instead of power."""
        return self._scale not in (
            Scale.MINUTE.value,
            Scale.SECOND.value,
            Scale.MINUTES_15.value,
        )

    def scale_readable(self):
        """Return a human readable scale."""
        if self._scale == Scale.MINUTE.value:
            return "Minute Average"
        if self._scale == Scale.DAY.value:
            return "Today"
        if self._scale == Scale.MONTH.value:
            return "This Month"
        return self._scale


class CurrentVueVoltageSensor(
    EmporiaLocationEntityMixin, CoordinatorEntity, SensorEntity
):  # type: ignore
    """Representation of a Vue Sensor's split-phase voltage."""

    def __init__(
        self,
        coordinator,
        identifier: str,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._explicit_site_map = site_map
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]
        final_channel: VueDeviceChannel | None = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    final_channel = channel
                    break
        if final_channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        self._channel: VueDeviceChannel = final_channel

        self._attr_has_entity_name = True
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

        if channel_num in SPLIT_PHASE_MAINS_CHANNELS:
            leg = channel_num.replace("Mains_", "")
            self._attr_translation_key = "mains_voltage_minute_average"
            self._attr_translation_placeholders = {"leg": leg}
            self._attr_name = f"Mains {leg} voltage minute average"
        else:
            self._attr_name = f"{channel_num} voltage minute average"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device.device_gid}-1,2,3")},
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
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            voltage = self.coordinator.data[self._id].get("voltage")
            return float(voltage) if voltage is not None else None
        return None

    @property
    def unique_id(self) -> str:
        """Return the Unique ID for the sensor."""
        device_gid = self._channel.device_gid or self._device.device_gid
        return f"sensor.emporia_vue.voltage.{device_gid}-{self._channel.channel_num}"


class EmporiaUpdateTelemetrySensor(SensorEntity):
    """Base entity for Emporia cloud update telemetry."""

    _attr_should_poll = False

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        listener_names: set[str] | None = None,
    ) -> None:
        """Initialize an update telemetry sensor."""
        self._update_methods = update_methods
        self._listener_names = listener_names

    async def async_added_to_hass(self) -> None:
        """Subscribe to update telemetry changes."""
        await super().async_added_to_hass()
        for name, update_method in self._update_methods.items():
            if self._listener_names is not None and name not in self._listener_names:
                continue
            self.async_on_remove(update_method.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Write update telemetry changes to Home Assistant."""
        self.async_write_ha_state()


class EmporiaApiRetrySensor(EmporiaUpdateTelemetrySensor):
    """Expose cloud retry telemetry in Home Assistant."""

    _attr_icon = "mdi:cloud-refresh"
    _attr_name = "Emporia API Retries"
    _attr_native_unit_of_measurement = "retries"

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        entry_id: str,
    ) -> None:
        """Initialize the API retry sensor."""
        super().__init__(update_methods)
        self._attr_unique_id = f"sensor.emporia_vue.api_retries.{entry_id}"

    @property
    def native_value(self) -> int:
        """Return total retries across enabled telemetry coordinators."""
        return sum(
            update_method.total_failures
            for update_method in self._update_methods.values()
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return per-coordinator retry details."""
        last_failures = [
            update_method.last_failure
            for update_method in self._update_methods.values()
            if update_method.last_failure is not None
        ]
        return {
            "retry_totals": {
                name: update_method.total_failures
                for name, update_method in self._update_methods.items()
            },
            "consecutive_retries": {
                name: update_method.consecutive_failures
                for name, update_method in self._update_methods.items()
            },
            "last_retry": max(last_failures).isoformat() if last_failures else None,
        }


class EmporiaApiLatencySensor(EmporiaUpdateTelemetrySensor):
    """Expose end-to-end Emporia API update latency."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_icon = "mdi:timer-outline"
    _attr_name = "Emporia API Latency"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        entry_id: str,
    ) -> None:
        """Initialize the API latency sensor."""
        super().__init__(update_methods, listener_names={"minute"})
        self._attr_unique_id = f"sensor.emporia_vue.api_latency.{entry_id}"

    @property
    def native_value(self) -> float | None:
        """Return the latest minute telemetry update duration in milliseconds."""
        minute_update = self._update_methods.get("minute")
        if not minute_update or minute_update.last_duration_ms is None:
            return None
        return round(minute_update.last_duration_ms, 1)

    @property
    def extra_state_attributes(self) -> dict:
        """Return per-coordinator update durations."""
        minute_update = self._update_methods["minute"]
        return {
            "update_latency_ms": {
                name: round(update_method.last_duration_ms, 1)
                for name, update_method in self._update_methods.items()
                if update_method.last_duration_ms is not None
            },
            "last_update_attempt": (
                minute_update.last_attempt.isoformat()
                if minute_update.last_attempt
                else None
            ),
            "measurement": "end_to_end_update_duration",
        }


# Known Emporia charger API responses (from historical data):
#   Status: "Charging", "Standby", "DeviceNotConnected", ""
#   Messages: "Charging", "Ready", "Off", "Self Test", "Offline",
#             "EV is not accepting charge", "Connected to EV",
#             "Please Wait", "Charging Halted", ""


CHARGER_STATUS_OPTIONS = ["Disconnected", "Connected", "Charging", "Error"]


class EmporiaChargerStatusSensor(
    EmporiaLocationEntityMixin, CoordinatorEntity, SensorEntity
):  # type: ignore
    """Representation of an Emporia Charger status sensor."""

    def __init__(
        self,
        coordinator,
        device: VueDevice,
        device_information: dict[int, VueDevice] | None = None,
        site_map: dict[int, Any] | None = None,
    ) -> None:
        """Initialize the charger status sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_gid = str(device.device_gid)
        self._device_information = device_information or {}
        self._explicit_site_map = site_map
        self._attr_has_entity_name = True
        self._attr_name = "Status"
        self._attr_translation_key = "charger_status"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = CHARGER_STATUS_OPTIONS
        self._attr_icon = "mdi:ev-station"

    def _target_load_gid(self, data: ChargerDevice) -> int:
        ev_charger = getattr(self._device, "ev_charger", None)
        gid = (
            getattr(self._device, "load_gid", None)
            or getattr(data, "load_gid", None)
            or getattr(ev_charger, "load_gid", None)
            or self._device_gid
        )
        try:
            return int(gid)
        except (ValueError, TypeError):
            return int(self._device_gid)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        return not (data is None or is_charger_offline(data))

    @property
    def native_value(self) -> str | None:
        """Return the human-friendly charger status."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if data:
            state, _ = evaluate_iec_charger_status(data)
            return state
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return canonical operational, IEC, and session attributes."""
        attrs = dict(super().extra_state_attributes or {})
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if not data:
            return attrs

        excess_enabled, _ = resolve_excess_solar_participation(
            self.coordinator.data,
            self._device_information,
            self._target_load_gid(data),
            int(self._device_gid),
            fallback_holder=data,
        )

        _, iec_code = evaluate_iec_charger_status(data)
        substatus = evaluate_charger_substatus(data, excess_enabled)

        evse_control = getattr(data, "evse_control", None)

        attrs.update(
            {
                "iec_status": iec_code,
                "raw_status": resolve_effective_charger_status(data) or data.status,
                "raw_message": data.message,
                "substatus": substatus,
                "session_active": bool(getattr(evse_control, "session_active", False))
                if evse_control is not None
                else False,
            }
        )

        if data.fault_text:
            attrs["fault_text"] = data.fault_text
        if evse_control is not None:
            if getattr(evse_control, "fault_code", None) is not None:
                attrs["fault_code"] = evse_control.fault_code
            if getattr(evse_control, "cost", None) is not None:
                attrs["session_cost"] = evse_control.cost
            if getattr(evse_control, "active_charging_minutes", None) is not None:
                attrs["active_charging_minutes"] = evse_control.active_charging_minutes
            plug_in = getattr(evse_control, "plug_in", None)
            if plug_in is not None:
                attrs["plug_in_time"] = (
                    plug_in.isoformat()
                    if hasattr(plug_in, "isoformat")
                    else str(plug_in)
                )

        return attrs

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger status sensor."""
        return f"emporia_vue.charger_status_{self._device_gid}"

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
