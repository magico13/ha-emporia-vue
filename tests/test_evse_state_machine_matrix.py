"""Master 24-state truth matrix test verifying EVSE controls and sensors across all operational states."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pyemvue.device import (
    ChargerDevice,
    EnergyMonitorExcessGeneration,
    EvseControlStatus,
    ExcessGenerationLoad,
    VueDevice,
)

from custom_components.emporia_vue.button import (
    EmporiaChargerFullPowerOverrideButton,
)
from custom_components.emporia_vue.charger_entity import (
    evaluate_charger_substatus,
    evaluate_charger_switch_available,
    evaluate_charger_switch_is_on,
    evaluate_full_power_override_available,
    evaluate_iec_charger_status,
    evaluate_operating_state,
    resolve_excess_solar_participation,
)
from custom_components.emporia_vue.number import EmporiaChargerCurrentNumber
from custom_components.emporia_vue.sensor import (
    EmporiaChargerStatusSensor,
)
from custom_components.emporia_vue.switch import EmporiaChargerSwitch


@pytest.fixture
def base_evse_device() -> tuple[VueDevice, ChargerDevice]:
    """Return a baseline VueDevice and ChargerDevice."""
    vue_dev = VueDevice(gid=1003, modelNum="EVSE-001", firmwareVersion="2.1.0")
    vue_dev.device_name = "Garage EVSE"
    vue_dev.manufacturer_id = "EVSE-MANUF-1003"
    vue_dev.parent_device_gid = 1001

    charger_dev = ChargerDevice(gid=1003, on=True)
    charger_dev.charging_rate = 40
    charger_dev.max_charging_rate = 48
    charger_dev.status = "CONNECTED_ON"
    charger_dev.message = "Connected to EV"
    charger_dev.fault_text = None
    charger_dev.pro_control_code = ""

    vue_dev.ev_charger = charger_dev
    return vue_dev, charger_dev


MATRIX_PARAMS = [
    # state_id, status, subtext, subtext_params, rate_ctrl, button_behavior, session_active, pro_code, fault_code, excess_solar_enabled,
    # exp_switch_avail, exp_switch_is_on, exp_btn_avail, exp_num_avail, exp_iec_state, exp_iec_code, exp_op_state, exp_offered_amps
    (
        "S01_fault", "FAULTING", None, [], None, None, False, "", 102, False,
        True, False, False, True, "Error", "F", "fault", None,
    ),
    (
        "S02_offline", "OFFLINE", "OFFLINE", [], None, None, False, "", None, False,
        False, False, False, False, "Disconnected", "A", "offline", None,
    ),
    (
        "S03_off", "DISCONNECTED_OFF", None, [], None, None, False, "", None, False,
        True, False, False, True, "Disconnected", "A", "off", 0.0,
    ),
    (
        "S04_armed", "DISCONNECTED_ON", None, [], None, None, False, "", None, False,
        True, True, False, True, "Disconnected", "A", "armed", 0.0,
    ),
    (
        "S05_ready", "CONNECTED_OFF", None, [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "ready", 0.0,
    ),
    (
        "S06_ready_excess_solar", "CONNECTED_OFF", None, [], None, None, False, "", None, True,
        True, False, False, True, "Connected", "B", "ready_excess_solar", 0.0,
    ),
    (
        "S07_offering_charge", "CONNECTED_ON", None, [], None, None, False, "", None, False,
        True, True, False, True, "Connected", "B", "offering_charge", 48.0,
    ),
    (
        "S07_offering_charge_excess_solar", "CONNECTED_ON", None, [], "EXCESS_SOLAR", None, False, "", None, True,
        True, True, True, True, "Connected", "B", "offering_charge", 48.0,
    ),
    (
        "S08_waiting_for_solar", "CONNECTED_ON", "WAITING_FOR_SOLAR", [], "EXCESS_SOLAR", "OVERRIDE_EXCESS_SOLAR", False, "", None, True,
        True, True, True, True, "Connected", "B", "waiting_for_solar", 48.0,
    ),
    (
        "S09_idle", "CONNECTED_ON", "VEHICLE_STOPPED", [], None, None, True, "", None, False,
        True, True, False, True, "Connected", "B", "idle", 48.0,
    ),
    (
        "S10_vehicle_ended_charge", "CONNECTED_OFF", "VEHICLE_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "vehicle_ended_charge", 0.0,
    ),
    (
        "S11_charging", "CHARGING", None, [], None, None, True, "", None, False,
        True, True, False, True, "Charging", "C", "charging", 48.0,
    ),
    (
        "S12_charging_excess_solar", "CHARGING", None, [], "EXCESS_SOLAR", None, True, "", None, True,
        True, True, True, True, "Charging", "C", "charging_excess_solar", 24.0,
    ),
    (
        "S13_charging_peak_demand", "CHARGING", None, [], "PEAK_DEMAND", None, True, "", None, False,
        True, True, True, True, "Charging", "C", "charging_peak_demand", 16.0,
    ),
    (
        "S14_charging_power_smart", "CHARGING", None, [], "POWER_SMART", None, True, "", None, False,
        True, True, False, True, "Charging", "C", "charging_power_smart", 32.0,
    ),
    (
        "S15_charging_load_sharing", "CHARGING", None, [], "LOAD_SHARING", None, True, "", None, False,
        True, True, False, True, "Charging", "C", "charging_load_sharing", 20.0,
    ),
    (
        "S16_charging_full_power_override", "CHARGING", "EXCESS_SOLAR_PAUSED", [], None, None, True, "", None, True,
        True, True, False, True, "Charging", "C", "charging_full_power_override", 48.0,
    ),
    (
        "S17_paused", "CONNECTED_OFF", None, [], None, None, True, "", None, False,
        True, False, False, True, "Connected", "B", "paused", 0.0,
    ),
    (
        "S18_paused_manual", "CONNECTED_OFF", "MANUALLY_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "paused_manual", 0.0,
    ),
    (
        "S18_paused_manual_override_empty", "CONNECTED_OFF", "EXCESS_SOLAR_PAUSED", [], None, None, False, "", None, True,
        True, False, False, True, "Connected", "B", "paused_manual", 0.0,
    ),
    (
        "S19_paused_excess_solar_subtext", "CONNECTED_OFF", "EXCESS_SOLAR_PAUSED", ["2026-11-03T14:00:00Z"], None, None, False, "", None, True,
        True, False, False, True, "Connected", "B", "paused_excess_solar", 0.0,
    ),
    (
        "S19_paused_excess_solar_session_active", "CONNECTED_OFF", None, [], None, None, True, "", None, True,
        True, False, False, True, "Connected", "B", "paused_excess_solar", 0.0,
    ),
    (
        "S20_paused_peak_demand", "CONNECTED_OFF", "PEAK_DEMAND_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "paused_peak_demand", 0.0,
    ),
    (
        "S21_paused_schedule", "CONNECTED_OFF", "SCHEDULE_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "paused_schedule", 0.0,
    ),
    (
        "S22_paused_time_of_use", "CONNECTED_OFF", "TOU_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "paused_time_of_use", 0.0,
    ),
    (
        "S23_paused_utility", "CONNECTED_OFF", "UTILITY_STOPPED", [], None, None, False, "", None, False,
        True, False, False, True, "Connected", "B", "paused_utility", 0.0,
    ),
    (
        "S24_pro_control_locked", "CHARGING", None, [], None, None, True, "PARTNER_LOCK_123", None, False,
        True, True, False, True, "Charging", "C", "charging", 48.0,
    ),
]


@pytest.mark.parametrize(
    (
        "state_id, status, subtext, subtext_params, rate_ctrl, button_behavior, session_active, pro_code, fault_code, excess_solar_enabled, "
        "exp_switch_avail, exp_switch_is_on, exp_btn_avail, exp_num_avail, exp_iec_state, exp_iec_code, exp_op_state, exp_offered_amps"
    ),
    MATRIX_PARAMS,
)
def test_evse_master_truth_matrix(
    base_evse_device: tuple[VueDevice, ChargerDevice],
    state_id: str,
    status: str,
    subtext: str | None,
    subtext_params: list[str],
    rate_ctrl: str | None,
    button_behavior: str | None,
    session_active: bool,
    pro_code: str,
    fault_code: int | None,
    excess_solar_enabled: bool,
    exp_switch_avail: bool,
    exp_switch_is_on: bool,
    exp_btn_avail: bool,
    exp_num_avail: bool,
    exp_iec_state: str,
    exp_iec_code: str,
    exp_op_state: str,
    exp_offered_amps: float | None,
) -> None:
    """Parametrized verification of all 24 operational states against all controls and sensors."""
    vue_dev, charger_dev = base_evse_device
    charger_dev.status = status
    charger_dev.pro_control_code = pro_code

    evse_ctrl = EvseControlStatus(
        device_id="EVSE-MANUF-1003",
        charger_status=status,
        subtext_type=subtext,
        subtext_parameters=subtext_params,
        charging_rate_controller=rate_ctrl,
        button_behavior=button_behavior,
        session_active=session_active,
        fault_code=fault_code,
        offered_amps=exp_offered_amps if exp_offered_amps is not None else 48.0,
        energy_delivered=12.5 if session_active else None,
    )
    charger_dev.evse_control = evse_ctrl

    # Pure Evaluators
    assert evaluate_charger_switch_available(charger_dev) is exp_switch_avail
    assert evaluate_charger_switch_is_on(charger_dev) is exp_switch_is_on
    assert evaluate_full_power_override_available(charger_dev) is exp_btn_avail

    iec_state, iec_code = evaluate_iec_charger_status(charger_dev)
    assert iec_state == exp_iec_state
    assert iec_code == exp_iec_code

    op_state = evaluate_operating_state(charger_dev, excess_solar_enabled)
    assert op_state == exp_op_state

    # Entity Level Assertions
    coord = MagicMock()
    coord.data = {"1003": charger_dev}
    if excess_solar_enabled:
        mock_eg = EnergyMonitorExcessGeneration(device_id="1001")
        mock_eg.loads = [ExcessGenerationLoad(load_gid=1003, enabled=True)]
        coord.data["excess_solar_by_device_id"] = {"1001": mock_eg}

    # 1. Switch
    switch = EmporiaChargerSwitch(
        coord, MagicMock(), vue_dev, device_information={1003: vue_dev}
    )
    assert switch.available is exp_switch_avail
    assert switch.is_on is exp_switch_is_on
    assert "energy_managements_configured" not in switch.extra_state_attributes
    assert "excess_solar_enabled" not in switch.extra_state_attributes

    # Legacy status string mapping on switch for backward compatibility
    if state_id == "S02_offline":
        assert switch.extra_state_attributes["status"] == "DeviceNotConnected"
    elif exp_iec_state == "Charging":
        assert switch.extra_state_attributes["status"] == "Charging"
    else:
        assert switch.extra_state_attributes["status"] == "Standby"
    assert "substatus" not in switch.extra_state_attributes

    # 2. Number
    number = EmporiaChargerCurrentNumber(coord, MagicMock(), vue_dev)
    assert number.available is exp_num_avail

    # 3. Override Button
    button = EmporiaChargerFullPowerOverrideButton(coord, MagicMock(), vue_dev)
    assert button.available is exp_btn_avail

    # 4. Status Sensor
    status_sensor = EmporiaChargerStatusSensor(
        coord, vue_dev, device_information={1003: vue_dev}
    )
    assert status_sensor.available is (state_id != "S02_offline")
    assert status_sensor.native_value == exp_iec_state
    attrs = status_sensor.extra_state_attributes
    assert attrs.get("iec_status") == exp_iec_code
    expected_substatus = evaluate_charger_substatus(charger_dev, excess_solar_enabled)
    assert attrs.get("substatus") == expected_substatus


def test_adversarial_multi_monitor_excess_solar_evaluation(
    base_evse_device: tuple[VueDevice, ChargerDevice],
) -> None:
    """Verify resolve_excess_solar_participation does not exit early on disabled monitor ordered first."""
    vue_dev, charger_dev = base_evse_device
    charger_dev.status = "CONNECTED_OFF"

    # Monitor 1 (ordered first): load 1003 is disabled
    eg_disabled = EnergyMonitorExcessGeneration(device_id="9001")
    eg_disabled.loads = [ExcessGenerationLoad(load_gid=1003, enabled=False)]

    # Monitor 2 (ordered second): load 1003 is enabled
    eg_enabled = EnergyMonitorExcessGeneration(device_id="9002")
    eg_enabled.loads = [ExcessGenerationLoad(load_gid=1003, enabled=True)]

    vue_mon1 = VueDevice(gid=9001, modelNum="VUE003")
    vue_mon1.device_name = "Subpanel Vue"
    vue_mon2 = VueDevice(gid=9002, modelNum="VUE003")
    vue_mon2.device_name = "Main Panel Vue"

    device_info = {
        1003: vue_dev,
        9001: vue_mon1,
        9002: vue_mon2,
    }

    coord_data = {
        "excess_solar_by_device_id": {
            "9001": eg_disabled,
            "9002": eg_enabled,
        }
    }

    enabled, managing_vue = resolve_excess_solar_participation(
        coord_data,
        device_info,
        target_load_gid=1003,
        device_gid=1003,
    )

    assert enabled is True
    assert managing_vue == "Main Panel Vue"

    # Evaluate operating state
    op_state = evaluate_operating_state(charger_dev, enabled)
    assert op_state == "ready_excess_solar"
