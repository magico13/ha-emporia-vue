"""Tests for EVSE session telemetry sensors (Session energy, Charging current, Charging power)."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from pyemvue.device import ChargerDevice, EvseControlStatus, VueDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.sensor import (
    EmporiaChargerCurrentSensor,
    EmporiaChargerPowerSensor,
    EmporiaChargerSessionEnergySensor,
    EmporiaChargerStatusSensor,
)
from custom_components.emporia_vue.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)


@pytest.fixture
def mock_evse_device() -> tuple[VueDevice, ChargerDevice]:
    """Return a mock EVSE VueDevice and ChargerDevice pair."""
    vue_device = VueDevice(
        gid=1003,
        modelNum="EVSE-001",
        firmwareVersion="2.1.0",
    )
    vue_device.device_name = "Garage Charger"
    vue_device.manufacturer_id = "MANUF-EVSE-1003"
    vue_device.ev_charger = True

    charger_device = ChargerDevice(gid=1003, on=True)
    charger_device.charging_rate = 32
    charger_device.max_charging_rate = 48
    charger_device.status = "Charging"
    charger_device.message = "Vehicle Connected"
    charger_device.fault_text = None

    vue_device.ev_charger = charger_device
    return vue_device, charger_device


async def test_evse_session_energy_sensor_metrics_and_device_info(
    mock_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Test EmporiaChargerSessionEnergySensor state, unique_id, unit, and device info."""
    vue_dev, charger_dev = mock_evse_device
    charger_dev.evse_control = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        energy_delivered=24.68,
    )

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    sensor = EmporiaChargerSessionEnergySensor(coord, vue_dev)

    assert sensor.native_value == 24.68
    assert sensor.unique_id == "sensor.emporia_vue.session_energy.1003"
    assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    assert sensor.name == "Session energy"

    device_info = sensor.device_info
    assert device_info["identifiers"] == {(DOMAIN, "1003-1,2,3")}
    assert "via_device" not in device_info
    assert device_info["name"] == "Garage Charger"
    assert device_info["model"] == "EVSE-001"
    assert device_info["serial_number"] == "MANUF-EVSE-1003"

    # Test None state when evse_control or energy_delivered is absent
    charger_dev.evse_control = None
    assert sensor.native_value is None

    charger_dev.evse_control = EvseControlStatus(energy_delivered=None)
    assert sensor.native_value is None


async def test_evse_charging_current_sensor_metrics_and_device_info(
    mock_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Test EmporiaChargerCurrentSensor state, unique_id, unit, and device info."""
    vue_dev, charger_dev = mock_evse_device
    charger_dev.evse_control = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        charging_rate_amps=32.0,
    )

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    sensor = EmporiaChargerCurrentSensor(coord, vue_dev)

    assert sensor.native_value == 32
    assert sensor.suggested_display_precision == 0
    assert sensor.unique_id == "sensor.emporia_vue.charging_current.1003"
    assert sensor.native_unit_of_measurement == UnitOfElectricCurrent.AMPERE
    assert sensor.device_class == SensorDeviceClass.CURRENT
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.name == "Charging current"
    assert sensor.icon == "mdi:current-ac"

    device_info = sensor.device_info
    assert device_info["identifiers"] == {(DOMAIN, "1003-1,2,3")}
    assert "via_device" not in device_info
    assert device_info["name"] == "Garage Charger"
    assert device_info["model"] == "EVSE-001"
    assert device_info["serial_number"] == "MANUF-EVSE-1003"

    # Test idle/0 state when charging_rate_amps is None
    charger_dev.evse_control = EvseControlStatus(charging_rate_amps=None)
    assert sensor.native_value == 0

    # Test 0 state when evse_control is absent
    charger_dev.evse_control = None
    assert sensor.native_value == 0

    # Test None state when device data is not in coordinator
    coord.data = {}
    assert sensor.native_value is None


async def test_evse_charging_power_sensor_metrics_and_device_info(
    mock_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Test EmporiaChargerPowerSensor state, unique_id, unit, and device info."""
    vue_dev, charger_dev = mock_evse_device
    charger_dev.evse_control = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        charging_rate_kw=7.681,
    )

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    sensor = EmporiaChargerPowerSensor(coord, vue_dev)

    assert sensor.native_value == 7.681
    assert sensor.suggested_display_precision == 2
    assert sensor.unique_id == "sensor.emporia_vue.charging_power.1003"
    assert sensor.native_unit_of_measurement == UnitOfPower.KILO_WATT
    assert sensor.device_class == SensorDeviceClass.POWER
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.name == "Charging power"
    assert sensor.icon == "mdi:lightning-bolt"

    device_info = sensor.device_info
    assert device_info["identifiers"] == {(DOMAIN, "1003-1,2,3")}
    assert "via_device" not in device_info
    assert device_info["name"] == "Garage Charger"
    assert device_info["model"] == "EVSE-001"
    assert device_info["serial_number"] == "MANUF-EVSE-1003"

    # Test idle/0.0 state when charging_rate_kw is None
    charger_dev.evse_control = EvseControlStatus(charging_rate_kw=None)
    assert sensor.native_value == 0.0

    # Test 0.0 state when evse_control is absent
    charger_dev.evse_control = None
    assert sensor.native_value == 0.0

    # Test None state when device data is not in coordinator
    coord.data = {}
    assert sensor.native_value is None


async def test_evse_sensor_async_setup_entry_registers_charger_sensors(
    hass: HomeAssistant, mock_evse_device: tuple[VueDevice, ChargerDevice]
) -> None:
    """Test sensor setup entry adds status, session energy, charging current, and charging power sensors."""
    vue_dev, charger_dev = mock_evse_device
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_1min": None,
        "coordinator_1mon": None,
        "coordinator_day_sensor": None,
        "coordinator_device_status": coord,
        "device_information": {1003: vue_dev},
        "retry_update_methods": {},
    }

    added_entities = []

    def async_add_entities(entities: list, update_before_add: bool = False) -> None:
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 4
    status_sensor = next(
        e for e in added_entities if isinstance(e, EmporiaChargerStatusSensor)
    )
    energy_sensor = next(
        e for e in added_entities if isinstance(e, EmporiaChargerSessionEnergySensor)
    )
    current_sensor = next(
        e for e in added_entities if isinstance(e, EmporiaChargerCurrentSensor)
    )
    power_sensor = next(
        e for e in added_entities if isinstance(e, EmporiaChargerPowerSensor)
    )
    assert status_sensor.unique_id == "emporia_vue.charger_status_1003"
    assert energy_sensor.unique_id == "sensor.emporia_vue.session_energy.1003"
    assert current_sensor.unique_id == "sensor.emporia_vue.charging_current.1003"
    assert power_sensor.unique_id == "sensor.emporia_vue.charging_power.1003"
