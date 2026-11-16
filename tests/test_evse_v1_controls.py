"""Tests for EVSE backend modernization, V1 protocol dispatch, and session telemetry."""

import inspect
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pyemvue.device import ChargerDevice, EvseControlStatus, VueDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue.const import (
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
)
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
    assert attrs["active_charging_minutes"] == 92
    assert "session_cost" not in attrs
    assert "plug_in_time" not in attrs

    # Test fallback when evse_control is None
    charger_dev.evse_control = None
    attrs_none = sensor.extra_state_attributes
    assert attrs_none["iec_status"] == "C"
    assert attrs_none["substatus"] == "Standard"
    assert "active_charging_minutes" not in attrs_none


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
    """Test real __init__ device_status coordinator, error recovery, and set_charger_current service."""
    import custom_components.emporia_vue as ev

    vue_dev, charger_dev = mock_evse_device
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password",
            ENABLE_1M: False,
            ENABLE_1D: False,
            ENABLE_1MON: False,
        },
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    vue_mock = MagicMock()
    vue_mock.get_devices_v1.return_value = [vue_dev]
    vue_mock.get_sites.return_value = []
    vue_mock.get_devices_status.return_value = ([], [charger_dev])
    vue_mock.get_evse_control.return_value = EvseControlStatus(
        device_id="MANUF-EVSE-1003",
        energy_delivered=18.5,
        cost=2.5,
    )
    vue_mock.set_evse_settings.return_value = True

    ev.DEVICE_GIDS.clear()
    ev.DEVICE_INFORMATION.clear()

    try:
        with (
            patch.object(ev, "async_login_vue", AsyncMock(return_value=True)),
            patch.object(ev, "PyEmVue", return_value=vue_mock),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(return_value=True),
            ),
        ):
            assert await ev.async_setup_entry(hass, entry) is True

            coord = hass.data[DOMAIN][entry.entry_id]["coordinator_device_status"]
            assert coord is not None

            # 1. Verify coordinator attaches evse_control using MANUF-EVSE-1003
            assert "1003" in coord.data
            assert coord.data["1003"].evse_control is not None
            assert coord.data["1003"].evse_control.energy_delivered == 18.5
            vue_mock.get_evse_control.assert_called_once_with("MANUF-EVSE-1003")

            # 2. Verify error handling: when get_evse_control raises, update completes cleanly
            vue_mock.get_evse_control.side_effect = RuntimeError("API timeout")
            await coord.async_refresh()
            assert coord.last_update_success is True
            assert coord.data["1003"].evse_control is None

            # 3. Exercise real DOMAIN.set_charger_current service
            ent_reg = er.async_get(hass)
            reg_entry = ent_reg.async_get_or_create(
                "switch",
                DOMAIN,
                "charger.emporia_vue.1003",
                suggested_object_id="garage_charger",
                config_entry=entry,
            )
            hass.states.async_set(reg_entry.entity_id, "on", {"charging_rate": 32})

            initial_status_calls = vue_mock.get_devices_status.call_count
            with patch.object(
                coord, "async_request_refresh", wraps=coord.async_request_refresh
            ) as mock_refresh:
                await hass.services.async_call(
                    DOMAIN,
                    "set_charger_current",
                    {"entity_id": reg_entry.entity_id, "current": 40},
                    blocking=True,
                )
                vue_mock.set_evse_settings.assert_called_once_with(
                    "MANUF-EVSE-1003", 40
                )
                # No local state/model bypass mutation before coordinator refresh
                assert charger_dev.charging_rate == 32
                assert (
                    hass.states.get(reg_entry.entity_id).attributes["charging_rate"]
                    == 32
                )
                assert (
                    vue_mock.get_devices_status.call_count == initial_status_calls + 1
                )

                # Verify [6, max_charging_rate] clamping
                vue_mock.set_evse_settings.reset_mock()
                await hass.services.async_call(
                    DOMAIN,
                    "set_charger_current",
                    {"entity_id": reg_entry.entity_id, "current": 80},
                    blocking=True,
                )
                vue_mock.set_evse_settings.assert_called_once_with(
                    "MANUF-EVSE-1003", 48
                )

                vue_mock.set_evse_settings.reset_mock()
                await hass.services.async_call(
                    DOMAIN,
                    "set_charger_current",
                    {"entity_id": reg_entry.entity_id, "current": 2},
                    blocking=True,
                )
                vue_mock.set_evse_settings.assert_called_once_with("MANUF-EVSE-1003", 6)

                # Verify invalid target raises ServiceValidationError
                with pytest.raises(ServiceValidationError):
                    await hass.services.async_call(
                        DOMAIN,
                        "set_charger_current",
                        {"entity_id": "switch.nonexistent_charger", "current": 16},
                        blocking=True,
                    )

                # Verify legacy fallback path via real service handler
                del vue_mock.set_evse_settings
                vue_mock.update_charger.return_value = charger_dev
                await hass.services.async_call(
                    DOMAIN,
                    "set_charger_current",
                    {"entity_id": reg_entry.entity_id, "current": 24},
                    blocking=True,
                )
                vue_mock.update_charger.assert_called_once_with(charger_dev, True, 24)
                assert mock_refresh.call_count == 4
    finally:
        while entry._on_unload:
            unsub = entry._on_unload.pop()
            res = unsub()
            if inspect.isawaitable(res):
                await res
        ev.DEVICE_GIDS.clear()
        ev.DEVICE_INFORMATION.clear()
