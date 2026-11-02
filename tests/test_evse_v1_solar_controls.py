"""Tests for EVSE excess solar configuration controls and full power override."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pyemvue.device import (
    ChargerDevice,
    EnergyMonitorExcessGeneration,
    ExcessGenerationLoad,
    ExcessGenerationMonitor,
    ExcessGenerationMonitors,
    OutletDevice,
    VueDevice,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests import Response, exceptions

from custom_components.emporia_vue.button import (
    EmporiaChargerFullPowerOverrideButton,
)
from custom_components.emporia_vue.button import (
    async_setup_entry as button_async_setup_entry,
)
from custom_components.emporia_vue.const import DOMAIN, VUE_DATA
from custom_components.emporia_vue.switch import (
    EmporiaChargerSwitch,
    EmporiaOutletSwitch,
    EmporiaVueExcessSolarLoadSwitch,
)
from custom_components.emporia_vue.switch import (
    async_setup_entry as switch_async_setup_entry,
)


@pytest.fixture
def mock_devices() -> tuple[VueDevice, ChargerDevice, VueDevice, VueDevice, VueDevice]:
    """Return a mock EVSE, 2 solar Vue monitors, and 1 non-solar Vue monitor."""
    # Charger
    vue_evse = VueDevice(gid=1003, modelNum="EVSE-001", firmwareVersion="2.1.0")
    vue_evse.device_name = "Garage Charger"
    vue_evse.manufacturer_id = "MANUF-EVSE-1003"
    vue_evse.parent_device_gid = 1001

    charger_dev = ChargerDevice(gid=1003, on=True)
    charger_dev.charging_rate = 32
    charger_dev.max_charging_rate = 48
    vue_evse.ev_charger = charger_dev

    # Monitor 1 (Main panel with solar)
    vue_mon1 = VueDevice(gid=1001, modelNum="VUE003")
    vue_mon1.device_name = "Main Panel"
    vue_mon1.manufacturer_id = "MANUF-MON-1001"

    # Monitor 2 (Subpanel with solar)
    vue_mon2 = VueDevice(gid=1002, modelNum="VUE002")
    vue_mon2.device_name = "Subpanel"
    vue_mon2.manufacturer_id = "MANUF-MON-1002"

    # Monitor 3 (Garage panel without solar)
    vue_mon3 = VueDevice(gid=1004, modelNum="VUE002")
    vue_mon3.device_name = "Garage Subpanel"
    vue_mon3.manufacturer_id = "MANUF-MON-1004"

    return vue_evse, charger_dev, vue_mon1, vue_mon2, vue_mon3


@pytest.fixture
def mock_excess_solar_data():
    """Return mock ExcessGenerationMonitors and EnergyMonitorExcessGeneration configs."""
    excess_monitors = ExcessGenerationMonitors(
        monitors=[
            ExcessGenerationMonitor(device_id="MANUF-MON-1001", enabled=True),
            ExcessGenerationMonitor(device_id="MANUF-MON-1002", enabled=False),
        ],
        unavailable_monitors=["MANUF-MON-1004"],
    )

    mon1_config = EnergyMonitorExcessGeneration(
        device_id="MANUF-MON-1001",
        loads=[
            ExcessGenerationLoad(load_gid=1003, enabled=True),
        ],
        solar_window={"start": {"anchor": "SUNRISE"}, "end": {"anchor": "SUNSET"}},
        sunrise="2026-11-02T14:00:00Z",
        sunset="2026-11-03T00:00:00Z",
    )

    mon2_config = EnergyMonitorExcessGeneration(
        device_id="MANUF-MON-1002",
        loads=[
            ExcessGenerationLoad(load_gid=1003, enabled=False),
        ],
    )

    return excess_monitors, {
        "MANUF-MON-1001": mon1_config,
        "MANUF-MON-1002": mon2_config,
    }


async def test_excess_solar_load_switch_properties_and_turn_on_off(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test EmporiaVueExcessSolarLoadSwitch properties, is_on, and turn_on/off."""
    vue_evse, charger_dev, vue_mon1, vue_mon2, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }
    coord.async_request_refresh = AsyncMock()

    device_info = {
        1001: vue_mon1,
        1002: vue_mon2,
        1003: vue_evse,
    }

    vue = MagicMock()
    vue.set_excess_generation.return_value = True

    # Switch on Main Panel (1001) for EV Charger (1003)
    switch = EmporiaVueExcessSolarLoadSwitch(
        coord,
        vue,
        vue_mon1,
        "MANUF-MON-1001",
        1003,
        "Garage Charger",
        device_info,
    )
    switch.hass = hass

    assert switch.unique_id == "emporia_vue.excess_solar_1001_1003"
    assert switch.entity_category == EntityCategory.CONFIG
    assert switch.name == "Excess solar Garage Charger"
    assert switch.icon == "mdi:solar-power"
    assert switch.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}
    assert switch.is_on is True

    # Turn off: should dispatch load_gid=1003, enabled=False
    await switch.async_turn_off()
    assert vue.set_excess_generation.called
    args = vue.set_excess_generation.call_args[0]
    assert args[0] == "MANUF-MON-1001"
    dispatched_loads = args[1]
    assert len(dispatched_loads) == 1
    assert dispatched_loads[0].load_gid == 1003
    assert dispatched_loads[0].enabled is False
    coord.async_request_refresh.assert_awaited()

    # Reset mock and turn on
    vue.set_excess_generation.reset_mock()
    coord.async_request_refresh.reset_mock()
    await switch.async_turn_on()
    assert vue.set_excess_generation.called
    args = vue.set_excess_generation.call_args[0]
    assert args[0] == "MANUF-MON-1001"
    dispatched_loads = args[1]
    assert len(dispatched_loads) == 1
    assert dispatched_loads[0].load_gid == 1003
    assert dispatched_loads[0].enabled is True
    coord.async_request_refresh.assert_awaited()


async def test_excess_solar_load_switch_exclusivity_guard(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test Approach 2 exclusivity guard raises ServiceValidationError if load active on another Vue."""
    vue_evse, charger_dev, vue_mon1, vue_mon2, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    # Load 1003 is already enabled=True on MANUF-MON-1001
    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }
    device_info = {
        1001: vue_mon1,
        1002: vue_mon2,
        1003: vue_evse,
    }
    vue = MagicMock()

    # Switch on Subpanel (MANUF-MON-1002)
    switch_subpanel = EmporiaVueExcessSolarLoadSwitch(
        coord,
        vue,
        vue_mon2,
        "MANUF-MON-1002",
        1003,
        "Garage Charger",
        device_info,
    )
    switch_subpanel.hass = hass

    # Attempting to turn on on Subpanel while Main Panel is active must raise ServiceValidationError
    with pytest.raises(ServiceValidationError) as exc_info:
        await switch_subpanel.async_turn_on()

    assert exc_info.value.translation_key == "load_already_managed"
    assert exc_info.value.translation_placeholders["load"] == "Garage Charger"
    assert exc_info.value.translation_placeholders["vue"] == "Main Panel"
    # set_excess_generation must NOT have been called
    assert not vue.set_excess_generation.called


async def test_excess_solar_load_switch_catches_api_rejection(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test switch catches LOAD_ALREADY_CONTROLLED HTTP error and raises ServiceValidationError."""
    vue_evse, charger_dev, vue_mon1, vue_mon2, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    # Set both to disabled in local cache so exclusivity guard passes locally
    excess_solar["MANUF-MON-1001"].loads[0].enabled = False

    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }
    device_info = {
        1001: vue_mon1,
        1002: vue_mon2,
        1003: vue_evse,
    }

    # Simulate backend returning 400 LOAD_ALREADY_CONTROLLED
    resp = Response()
    resp.status_code = 400
    resp._content = b'{"error_code":"LOAD_ALREADY_CONTROLLED","details":[{"key":"LOAD_GID","value":"1003"}]}'
    http_err = exceptions.HTTPError("400 Client Error", response=resp)

    vue = MagicMock()
    vue.set_excess_generation.side_effect = http_err

    switch = EmporiaVueExcessSolarLoadSwitch(
        coord,
        vue,
        vue_mon2,
        "MANUF-MON-1002",
        1003,
        "Garage Charger",
        device_info,
    )
    switch.hass = hass

    with pytest.raises(ServiceValidationError) as exc_info:
        await switch.async_turn_on()

    assert exc_info.value.translation_key == "load_already_managed"


async def test_load_entities_state_attributes(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test EV charger and smart plug switches expose read-only excess solar attributes."""
    vue_evse, charger_dev, vue_mon1, vue_mon2, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    charger_dev.excess_monitors = excess_monitors
    charger_dev.excess_solar_by_device_id = excess_solar

    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }
    device_info = {
        1001: vue_mon1,
        1002: vue_mon2,
        1003: vue_evse,
    }
    vue = MagicMock()

    charger_switch = EmporiaChargerSwitch(coord, vue, vue_evse, None, None, device_info)
    charger_switch.hass = hass

    attrs = charger_switch.extra_state_attributes
    assert attrs["excess_solar_managing_vue"] == "Main Panel"
    assert "excess_solar_enabled" not in attrs
    assert "energy_managements_configured" not in attrs

    # Also test an outlet
    vue_plug = VueDevice(gid=1005, modelNum="OUTLET-001")
    vue_plug.device_name = "Pool Pump"
    vue_plug.load_gid = 1005
    outlet_dev = OutletDevice(gid=1005, on=True)
    vue_plug.outlet = outlet_dev

    coord.data["1005"] = outlet_dev
    device_info[1005] = vue_plug

    plug_switch = EmporiaOutletSwitch(coord, vue, "1005", vue_plug, device_info)
    plug_switch.hass = hass
    # 1005 is not enrolled in excess solar, so managing_vue is omitted
    plug_attrs = plug_switch.extra_state_attributes
    assert "excess_solar_managing_vue" not in plug_attrs
    assert "excess_solar_enabled" not in plug_attrs


async def test_capability_gating_suppresses_entities_when_no_solar_monitors(
    hass: HomeAssistant,
    mock_devices,
) -> None:
    """Test excess solar load switch is suppressed when no solar monitors exist."""
    vue_evse, charger_dev, _, _, _ = mock_devices
    # Non-solar account: excess_monitors has 0 monitors
    charger_dev.excess_monitors = ExcessGenerationMonitors(
        monitors=[], unavailable_monitors=["MANUF-MON-1004"]
    )
    charger_dev.excess_solar_by_device_id = {}

    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_device_status": coord,
        "device_information": {1003: vue_evse},
        VUE_DATA: MagicMock(),
    }

    # Switch platform setup
    switch_entities = []
    await switch_async_setup_entry(
        hass, entry, lambda entities: switch_entities.extend(entities)
    )
    # Only standard EmporiaChargerSwitch should be created, NOT EmporiaVueExcessSolarLoadSwitch
    assert len(switch_entities) == 1
    assert isinstance(switch_entities[0], EmporiaChargerSwitch)
    assert not any(
        isinstance(e, EmporiaVueExcessSolarLoadSwitch) for e in switch_entities
    )


async def test_capability_gating_creates_entities_when_solar_monitors_exist(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test excess solar switch is added on the Vue monitor when solar monitors exist."""
    vue_evse, charger_dev, vue_mon1, _, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data
    charger_dev.excess_monitors = excess_monitors
    charger_dev.excess_solar_by_device_id = excess_solar

    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_device_status": coord,
        "device_information": {1001: vue_mon1, 1003: vue_evse},
        VUE_DATA: MagicMock(),
    }

    switch_entities = []
    await switch_async_setup_entry(
        hass, entry, lambda entities: switch_entities.extend(entities)
    )
    # Should have EmporiaChargerSwitch on 1003, AND EmporiaVueExcessSolarLoadSwitch on 1001
    assert len(switch_entities) == 2
    assert any(isinstance(e, EmporiaChargerSwitch) for e in switch_entities)
    vue_switches = [
        e for e in switch_entities if isinstance(e, EmporiaVueExcessSolarLoadSwitch)
    ]
    assert len(vue_switches) == 1
    assert vue_switches[0].unique_id == "emporia_vue.excess_solar_1001_1003"


async def test_charge_at_full_power_override_button(
    hass: HomeAssistant,
    mock_devices,
) -> None:
    """Test EmporiaChargerFullPowerOverrideButton dispatches CHARGE_AT_FULL_POWER."""
    vue_evse, charger_dev, _, _, _ = mock_devices
    coord = MagicMock()
    coord.data = {"1003": charger_dev}
    coord.async_request_refresh = AsyncMock()

    vue = MagicMock()
    vue.post_evse_control.return_value = True

    button = EmporiaChargerFullPowerOverrideButton(coord, vue, vue_evse)
    button.hass = hass

    assert button.unique_id == "emporia_vue.charge_full_power_1003"
    assert button.name == "Charge at full power"
    assert button.icon == "mdi:lightning-bolt"
    # Button is primary control, not CONFIG category
    assert getattr(button, "entity_category", None) is None

    await button.async_press()
    vue.post_evse_control.assert_called_once_with(
        "MANUF-EVSE-1003", "CHARGE_AT_FULL_POWER"
    )
    coord.async_request_refresh.assert_awaited_once()


async def test_button_async_setup_entry_registers_button(
    hass: HomeAssistant,
    mock_devices,
) -> None:
    """Test button platform setup entry registers override button for EV charger."""
    vue_evse, charger_dev, _, _, _ = mock_devices
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    coord = MagicMock()
    coord.data = {"1003": charger_dev}

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_device_status": coord,
        "device_information": {1003: vue_evse},
        VUE_DATA: MagicMock(),
    }

    button_entities = []
    await button_async_setup_entry(
        hass, entry, lambda entities: button_entities.extend(entities)
    )
    assert len(button_entities) == 1
    assert isinstance(button_entities[0], EmporiaChargerFullPowerOverrideButton)
    assert button_entities[0].unique_id == "emporia_vue.charge_full_power_1003"


async def test_coordinator_fetches_excess_solar_data(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Test coordinator_device_status queries excess solar monitors and configs."""
    vue_evse, charger_dev, vue_mon1, _, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from custom_components.emporia_vue import DEVICE_INFORMATION

    DEVICE_INFORMATION[1003] = vue_evse
    DEVICE_INFORMATION[1001] = vue_mon1

    vue = MagicMock()
    vue.get_devices_status.return_value = ([], [charger_dev])
    vue.get_excess_generation_monitors.return_value = excess_monitors
    vue.get_excess_generation.side_effect = lambda dev_id: excess_solar.get(dev_id)
    vue.get_evse_control.return_value = None

    async def async_update_device_status():
        data = {}
        outlets, chargers = await hass.async_add_executor_job(vue.get_devices_status)
        excess_monitors_res = None
        excess_solar_by_device_id = {}
        if (outlets or chargers) and hasattr(vue, "get_excess_generation_monitors"):
            excess_monitors_res = await hass.async_add_executor_job(
                vue.get_excess_generation_monitors
            )
            if excess_monitors_res and excess_monitors_res.monitors:
                for mon in excess_monitors_res.monitors:
                    em_eg = await hass.async_add_executor_job(
                        vue.get_excess_generation, mon.device_id
                    )
                    if em_eg:
                        excess_solar_by_device_id[mon.device_id] = em_eg

        data["excess_monitors"] = excess_monitors_res
        data["excess_solar_by_device_id"] = excess_solar_by_device_id
        for charger in chargers:
            charger.excess_monitors = excess_monitors_res
            charger.excess_solar_by_device_id = excess_solar_by_device_id
            data[str(charger.device_gid)] = charger
        return data

    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    coord = DataUpdateCoordinator(
        hass,
        MagicMock(),
        config_entry=entry,
        name="device_status_test",
        update_method=async_update_device_status,
    )
    await coord.async_refresh()

    assert "1003" in coord.data
    polled_charger = coord.data["1003"]
    assert polled_charger.excess_monitors == excess_monitors
    assert "MANUF-MON-1001" in polled_charger.excess_solar_by_device_id
    assert (
        polled_charger.excess_solar_by_device_id["MANUF-MON-1001"].loads[0].load_gid
        == 1003
    )


async def test_excess_solar_controls_with_distinct_load_gid(
    hass: HomeAssistant,
    mock_devices,
    mock_excess_solar_data,
) -> None:
    """Verify excess solar switch when load_gid (9001) differs from device_gid (1003)."""
    vue_evse, charger_dev, vue_mon1, vue_mon2, vue_mon3 = mock_devices
    excess_monitors, excess_solar_configs = mock_excess_solar_data

    # Set distinct load_gid
    vue_evse.load_gid = 9001
    charger_dev.load_gid = 9001
    vue_evse.ev_charger = charger_dev

    excess_solar_configs["MANUF-MON-1001"].loads = [
        ExcessGenerationLoad(load_gid=9001, enabled=True)
    ]

    mock_coord = MagicMock()
    mock_coord.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar_configs,
    }
    mock_coord.async_request_refresh = AsyncMock()

    mock_vue = MagicMock()
    mock_vue.set_excess_generation = MagicMock()

    device_info_map = {
        1003: vue_evse,
        1001: vue_mon1,
        1002: vue_mon2,
        1004: vue_mon3,
    }

    switch = EmporiaVueExcessSolarLoadSwitch(
        mock_coord,
        mock_vue,
        vue_mon1,
        "MANUF-MON-1001",
        9001,
        "Garage Charger",
        device_info_map,
    )
    switch.hass = hass

    # Verify is_on returns True when matching 9001
    assert switch.is_on is True

    # Turn off: should dispatch load_gid=9001, enabled=False
    await switch.async_turn_off()
    assert mock_vue.set_excess_generation.called
    call_args = mock_vue.set_excess_generation.call_args[0]
    assert call_args[0] == "MANUF-MON-1001"
    dispatched_loads = call_args[1]
    assert len(dispatched_loads) == 1
    assert dispatched_loads[0].load_gid == 9001
    assert dispatched_loads[0].enabled is False

    mock_vue.set_excess_generation.reset_mock()

    # Turn on: should dispatch load_gid=9001, enabled=True
    await switch.async_turn_on()
    assert mock_vue.set_excess_generation.called
    call_args = mock_vue.set_excess_generation.call_args[0]
    assert call_args[0] == "MANUF-MON-1001"
    dispatched_loads = call_args[1]
    assert len(dispatched_loads) == 1
    assert dispatched_loads[0].load_gid == 9001
    assert dispatched_loads[0].enabled is True


async def test_platforms_ignore_non_digit_coordinator_keys(
    hass: HomeAssistant, mock_devices, mock_excess_solar_data
) -> None:
    """Test sensor, number, and button platform setup ignore non-digit coordinator keys."""
    vue_evse, charger_dev, _, _, _ = mock_devices
    excess_monitors, excess_solar = mock_excess_solar_data

    from custom_components.emporia_vue.number import (
        async_setup_entry as number_async_setup_entry,
    )
    from custom_components.emporia_vue.sensor import (
        async_setup_entry as sensor_async_setup_entry,
    )

    coord_status = MagicMock()
    coord_status.data = {
        "1003": charger_dev,
        "excess_monitors": excess_monitors,
        "excess_solar_by_device_id": excess_solar,
    }

    coord_usage = MagicMock()
    coord_usage.data = {}

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        entry.entry_id: {
            "coordinator_device_status": coord_status,
            "coordinator_1min": coord_usage,
            "coordinator_1hr": coord_usage,
            "coordinator_day": coord_usage,
            "coordinator_1mon": coord_usage,
            "coordinator_day_sensor": coord_usage,
            "retry_update_methods": {},
            VUE_DATA: MagicMock(),
            "device_information": {1003: vue_evse},
        }
    }

    # 1. Button setup
    buttons = []
    await button_async_setup_entry(hass, entry, buttons.extend)
    assert len(buttons) == 1

    # 2. Number setup
    numbers = []
    await number_async_setup_entry(hass, entry, numbers.extend)
    assert len(numbers) == 1

    # 3. Sensor setup (for charger status, session energy, charging current, and charging power)
    sensors = []
    await sensor_async_setup_entry(hass, entry, sensors.extend)
    assert len(sensors) == 4


async def test_charger_excess_solar_managing_vue_attribute(
    mock_devices: tuple[VueDevice, ChargerDevice, VueDevice, VueDevice, VueDevice],
    mock_excess_solar_data: tuple[
        ExcessGenerationMonitors, dict[str, EnergyMonitorExcessGeneration]
    ],
) -> None:
    """Validate EV charger extra_state_attributes exposes managing_vue and omits when None."""
    vue_evse, charger_dev, vue_mon1, _, _ = mock_devices
    _, excess_solar = mock_excess_solar_data

    coord = MagicMock()
    coord.data = {
        "1003": charger_dev,
        "excess_solar_by_device_id": excess_solar,
    }

    switch = EmporiaChargerSwitch(
        coord,
        MagicMock(),
        vue_evse,
        device_information={1003: vue_evse, 1001: vue_mon1},
    )

    attrs = switch.extra_state_attributes
    assert attrs["excess_solar_managing_vue"] == "Main Panel"
    assert "excess_solar_enabled" not in attrs
    assert "energy_managements_configured" not in attrs
    assert "peak_demand_configured" not in attrs
