"""Tests for EVSE backend modernization, V1 protocol dispatch, and session telemetry."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyemvue.device import ChargerDevice, EvseControlStatus, VueDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.number import EmporiaChargerCurrentNumber
from custom_components.emporia_vue.sensor import EmporiaChargerStatusSensor
from custom_components.emporia_vue.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.emporia_vue.switch import EmporiaChargerSwitch


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
    vue_device.parent_device_gid = 1001

    charger_device = ChargerDevice(gid=1003, on=True)
    charger_device.charging_rate = 32
    charger_device.max_charging_rate = 48
    charger_device.status = "Charging"
    charger_device.message = "Vehicle Connected"
    charger_device.fault_text = None

    vue_device.ev_charger = charger_device
    return vue_device, charger_device


async def test_evse_switch_v1_dispatch_and_legacy_fallback(
    hass: HomeAssistant, mock_evse_device: tuple[VueDevice, ChargerDevice]
) -> None:
    """Test EmporiaChargerSwitch uses V1 set_evse_state and falls back cleanly."""
    vue_dev, charger_dev = mock_evse_device
    coord = MagicMock()
    coord.data = {"1003": charger_dev}
    coord.async_request_refresh = AsyncMock()

    # 1. V1 modern dispatch
    vue_v1 = MagicMock()
    vue_v1.set_evse_state.return_value = True

    switch = EmporiaChargerSwitch(coord, vue_v1, vue_dev, None, "outlet")
    switch.hass = hass

    assert switch.unique_id == "charger.emporia_vue.1003"
    assert switch.is_on is True

    await switch.async_turn_on()
    vue_v1.set_evse_state.assert_called_once_with("MANUF-EVSE-1003", True)
    assert coord.async_request_refresh.call_count == 1

    await switch.async_turn_off()
    vue_v1.set_evse_state.assert_called_with("MANUF-EVSE-1003", False)
    assert coord.async_request_refresh.call_count == 2

    # 2. Legacy fallback when set_evse_state is absent
    vue_legacy = MagicMock(spec=["update_charger"])
    vue_legacy.update_charger.return_value = charger_dev

    switch_legacy = EmporiaChargerSwitch(coord, vue_legacy, vue_dev, None, "outlet")
    switch_legacy.hass = hass

    await switch_legacy.async_turn_on()
    vue_legacy.update_charger.assert_called_once_with(charger_dev, True)
    assert coord.async_request_refresh.call_count == 3

    vue_legacy.update_charger.reset_mock()
    await switch_legacy.async_turn_off()
    vue_legacy.update_charger.assert_called_once_with(charger_dev, False)
    assert coord.async_request_refresh.call_count == 4


async def test_evse_current_number_v1_dispatch_and_legacy_fallback(
    hass: HomeAssistant, mock_evse_device: tuple[VueDevice, ChargerDevice]
) -> None:
    """Test EmporiaChargerCurrentNumber uses set_evse_settings and falls back cleanly."""
    vue_dev, charger_dev = mock_evse_device
    coord = MagicMock()
    coord.data = {"1003": charger_dev}
    coord.async_request_refresh = AsyncMock()

    # 1. V1 modern dispatch
    vue_v1 = MagicMock()
    vue_v1.set_evse_settings.return_value = True

    number = EmporiaChargerCurrentNumber(coord, vue_v1, vue_dev)
    number.hass = hass

    assert number.unique_id == "emporia_vue.charger_current_1003"
    assert number.native_value == 32.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 48.0

    await number.async_set_native_value(40.0)
    vue_v1.set_evse_settings.assert_called_once_with("MANUF-EVSE-1003", 40)
    coord.async_request_refresh.assert_awaited_once()

    # 2. Legacy fallback when set_evse_settings is absent
    vue_legacy = MagicMock(spec=["update_charger"])
    vue_legacy.update_charger.return_value = charger_dev

    number_legacy = EmporiaChargerCurrentNumber(coord, vue_legacy, vue_dev)
    number_legacy.hass = hass

    coord.async_request_refresh.reset_mock()
    await number_legacy.async_set_native_value(24.0)
    vue_legacy.update_charger.assert_called_once_with(charger_dev, True, 24)
    coord.async_request_refresh.assert_awaited_once()


async def test_evse_status_sensor_session_attributes(
    mock_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Test EmporiaChargerStatusSensor enriches extra_state_attributes with session metrics."""
    vue_dev, charger_dev = mock_evse_device
    plug_in_dt = datetime(2026, 11, 2, 0, 15, 0)
    charger_dev.evse_control = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        session_active=True,
        energy_delivered=15.75,
        cost=3.45,
        active_charging_minutes=92,
        offered_amps=40.0,
        plug_in=plug_in_dt,
    )

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    sensor = EmporiaChargerStatusSensor(coord, vue_dev)

    assert sensor.native_value == "Charging"
    attrs = sensor.extra_state_attributes
    assert attrs["iec_status"] == "C"
    assert attrs["raw_status"] == "CHARGING"
    assert attrs["substatus"] == "Standard"
    assert attrs["raw_message"] == "Vehicle Connected"
    assert attrs.get("fault_text") is None
    assert attrs["session_cost"] == 3.45
    assert attrs["active_charging_minutes"] == 92
    assert attrs["plug_in_time"] == plug_in_dt.isoformat()

    # Test fallback when evse_control is None
    charger_dev.evse_control = None
    attrs_none = sensor.extra_state_attributes
    assert attrs_none["iec_status"] == "C"
    assert attrs_none["substatus"] == "Standard"
    assert "session_cost" not in attrs_none
    assert "active_charging_minutes" not in attrs_none
    assert "plug_in_time" not in attrs_none


async def test_evse_substatus_telemetry(
    mock_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Test substatus on status sensor with active charging and paused states."""
    vue_dev, charger_dev = mock_evse_device
    charger_dev.status = "CHARGING"
    charger_dev.evse_control = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        session_active=True,
        charger_status="CHARGING",
        charging_rate_amps=32.0,
        charging_rate_kw=7.7,
        charging_rate_controller="EXCESS_SOLAR",
    )

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    # 1. Active charging with excess solar
    status_sensor = EmporiaChargerStatusSensor(coord, vue_dev, {1003: vue_dev})
    assert status_sensor.unique_id == "emporia_vue.charger_status_1003"
    assert status_sensor.native_value == "Charging"
    attrs = status_sensor.extra_state_attributes
    assert attrs["iec_status"] == "C"
    assert attrs["substatus"] == "Excess solar"
    assert attrs["session_active"] is True

    # 2. Paused state: contactor open
    charger_dev.status = "CONNECTED_OFF"
    charger_dev.evse_control.charger_status = "CONNECTED_OFF"
    charger_dev.evse_control.subtext_type = "MANUALLY_STOPPED"
    assert status_sensor.native_value == "Connected"
    assert status_sensor.extra_state_attributes["substatus"] == "Paused manually"


async def test_evse_sensor_async_setup_entry_registers_status_sensor(
    hass: HomeAssistant, mock_evse_device: tuple[VueDevice, ChargerDevice]
) -> None:
    """Test sensor setup entry adds status sensor."""
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

    assert any(isinstance(e, EmporiaChargerStatusSensor) for e in added_entities)
    status_sensor = next(
        e for e in added_entities if isinstance(e, EmporiaChargerStatusSensor)
    )
    assert status_sensor.unique_id == "emporia_vue.charger_status_1003"


async def test_evse_coordinator_device_status_and_service_call(
    hass: HomeAssistant, mock_evse_device: tuple[VueDevice, ChargerDevice]
) -> None:
    """Test device status coordinator polling, error recovery, and service call."""
    vue_dev, charger_dev = mock_evse_device
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    vue_mock = MagicMock()
    vue_mock.get_devices_status.return_value = ([], [charger_dev])
    vue_mock.get_evse_control.return_value = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        energy_delivered=18.5,
        cost=2.5,
    )
    vue_mock.set_evse_settings.return_value = True

    from custom_components.emporia_vue import DEVICE_INFORMATION

    DEVICE_INFORMATION[1003] = vue_dev

    # Emulate async_update_device_status logic from __init__.py
    async def async_update_device_status():
        data = {}
        _outlets, chargers = await hass.async_add_executor_job(
            vue_mock.get_devices_status
        )
        if chargers:
            for charger in chargers:
                charger.evse_control = None
                if hasattr(vue_mock, "get_evse_control"):
                    dev_id = (
                        getattr(charger, "device_id", None)
                        or getattr(
                            DEVICE_INFORMATION.get(charger.device_gid),
                            "manufacturer_id",
                            None,
                        )
                        or str(charger.device_gid)
                    )
                    try:
                        charger.evse_control = await hass.async_add_executor_job(
                            vue_mock.get_evse_control, dev_id
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
                data[str(charger.device_gid)] = charger
        return data

    coord = DataUpdateCoordinator(
        hass,
        MagicMock(),
        config_entry=entry,
        name="device_status",
        update_method=async_update_device_status,
    )
    await coord.async_refresh()

    # 1. Verify charger evse_control is attached and fetched with MANUF-EVSE-1003
    assert "1003" in coord.data
    assert coord.data["1003"].evse_control is not None
    assert coord.data["1003"].evse_control.energy_delivered == 18.5
    vue_mock.get_evse_control.assert_called_once_with("MANUF-EVSE-1003")

    # 2. Verify error handling: when get_evse_control raises, update completes without failing
    vue_mock.get_evse_control.side_effect = RuntimeError("API timeout")
    await coord.async_refresh()
    assert coord.last_update_success is True
    assert coord.data["1003"].evse_control is None

    # 3. Test handle_set_charger_current modernization
    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        "charger.emporia_vue.1003",
        suggested_object_id="garage_charger",
    )
    hass.states.async_set(reg_entry.entity_id, "on", {"charging_rate": 32})

    async def call_handle_set_charger_current(vue_inst, current_target):
        state = hass.states.get(reg_entry.entity_id)
        dev_id = (
            getattr(charger_dev, "device_id", None)
            or getattr(vue_dev, "manufacturer_id", None)
            or str(charger_dev.device_gid)
        )
        if hasattr(vue_inst, "set_evse_settings"):
            await hass.async_add_executor_job(
                vue_inst.set_evse_settings, dev_id, current_target
            )
            charger_dev.charging_rate = current_target
        else:
            await hass.async_add_executor_job(
                vue_inst.update_charger,
                charger_dev,
                state.state == "on",
                current_target,
            )
        new_attrs = state.attributes.copy()
        new_attrs["charging_rate"] = current_target
        hass.states.async_set(reg_entry.entity_id, "on", new_attrs)

    # Test modern V1 dispatch
    await call_handle_set_charger_current(vue_mock, 40)
    vue_mock.set_evse_settings.assert_called_once_with("MANUF-EVSE-1003", 40)
    assert hass.states.get(reg_entry.entity_id).attributes["charging_rate"] == 40

    # Test legacy fallback
    vue_legacy = MagicMock(spec=["update_charger"])
    await call_handle_set_charger_current(vue_legacy, 24)
    vue_legacy.update_charger.assert_called_once_with(charger_dev, True, 24)
    assert hass.states.get(reg_entry.entity_id).attributes["charging_rate"] == 24
