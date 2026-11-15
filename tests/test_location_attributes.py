"""Tests for dynamic location entity state attributes."""

import inspect
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pyemvue.device import (
    ChargerDevice,
    OutletDevice,
    VueDevice,
    VueDeviceChannel,
    VueSite,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.emporia_vue import (
    _populate_site_map_and_assign_devices,
    async_setup_entry,
)
from custom_components.emporia_vue.charger_entity import (
    EmporiaLocationEntityMixin,
)
from custom_components.emporia_vue.const import DOMAIN, ENABLE_1D
from custom_components.emporia_vue.number import EmporiaChargerCurrentNumber
from custom_components.emporia_vue.sensor import (
    CurrentVuePowerSensor,
    CurrentVueVoltageSensor,
    EmporiaChargerStatusSensor,
)
from custom_components.emporia_vue.switch import (
    EmporiaChargerSwitch,
    EmporiaOutletSwitch,
)


@pytest.fixture
def site_assigned_vue_device() -> VueDevice:
    """Return a Vue device assigned to site 29372."""
    dev = VueDevice(gid=1001, modelNum="VUE003")
    dev.device_name = "Main Panel"
    dev.site_gid = 29372
    dev.channels = [
        VueDeviceChannel(
            gid=1001,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        )
    ]
    dev.channels[0].has_data = True
    return dev


@pytest.fixture
def unassigned_vue_device() -> VueDevice:
    """Return a Vue device unassigned to any site (site_gid=None)."""
    dev = VueDevice(gid=1002, modelNum="VUE003")
    dev.device_name = "Unassigned Panel"
    dev.site_gid = None
    dev.channels = [
        VueDeviceChannel(
            gid=1002,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        )
    ]
    dev.channels[0].has_data = True
    return dev


@pytest.fixture
def assigned_evse_device() -> tuple[VueDevice, ChargerDevice]:
    """Return an EVSE charger device assigned to site 29372."""
    dev = VueDevice(gid=2001, modelNum="EVSE-001")
    dev.device_name = "Garage Charger"
    dev.site_gid = 29372
    charger = ChargerDevice(gid=2001, on=True)
    charger.charging_rate = 32
    charger.max_charging_rate = 48
    dev.ev_charger = charger
    return dev, charger


@pytest.fixture
def assigned_outlet_device() -> tuple[VueDevice, OutletDevice]:
    """Return a smart plug device assigned to site 29372."""
    dev = VueDevice(gid=3001, modelNum="OUTLET-001")
    dev.device_name = "Living Room Plug"
    dev.site_gid = 29372
    outlet = OutletDevice(gid=3001, on=True)
    dev.outlet = outlet
    return dev, outlet


async def test_location_attributes_matching_site(
    hass: HomeAssistant,
    site_assigned_vue_device: VueDevice,
    assigned_evse_device: tuple[VueDevice, ChargerDevice],
    assigned_outlet_device: tuple[VueDevice, OutletDevice],
) -> None:
    """Test all base entities expose location when assigned to a known site."""
    site_map = {29372: "Primary Residence"}
    evse_dev, charger_data = assigned_evse_device
    outlet_dev, outlet_data = assigned_outlet_device

    # 1. Power sensor
    coord_usage = MagicMock()
    coord_usage.data = {
        "1001-1,2,3-1MIN": {
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "scale": "1MIN",
            "usage": 2.5,
            "info": site_assigned_vue_device,
        }
    }
    power_sensor = CurrentVuePowerSensor(
        coord_usage, "1001-1,2,3-1MIN", site_map=site_map
    )
    power_sensor.hass = hass
    attrs = power_sensor.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"

    # 2. Voltage sensor
    coord_usage.data["1001-Mains_A-1MIN"] = {
        "device_gid": 1001,
        "channel_num": "Mains_A",
        "voltage": 120.2,
        "info": site_assigned_vue_device,
    }
    site_assigned_vue_device.channels.append(
        VueDeviceChannel(
            gid=1001,
            channelNum="Mains_A",
            channelMultiplier=1.0,
            channelTypeGid=1,
        )
    )
    voltage_sensor = CurrentVueVoltageSensor(
        coord_usage, "1001-Mains_A-1MIN", site_map=site_map
    )
    voltage_sensor.hass = hass
    attrs = voltage_sensor.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"

    # 3. Charger switch
    coord_dev = MagicMock()
    coord_dev.data = {"2001": charger_data, "3001": outlet_data}
    charger_switch = EmporiaChargerSwitch(
        coord_dev,
        MagicMock(),
        evse_dev,
        None,
        SwitchDeviceClass.OUTLET,
        site_map=site_map,
    )
    charger_switch.hass = hass
    attrs = charger_switch.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"

    # 4. Charger current number
    number_entity = EmporiaChargerCurrentNumber(
        coord_dev, MagicMock(), evse_dev, site_map=site_map
    )
    number_entity.hass = hass
    attrs = number_entity.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"

    # 5. Smart plug outlet switch
    outlet_switch = EmporiaOutletSwitch(
        coord_dev, MagicMock(), "3001", outlet_dev, site_map=site_map
    )
    outlet_switch.hass = hass
    attrs = outlet_switch.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"

    # 6. Charger status sensor
    status_sensor = EmporiaChargerStatusSensor(coord_dev, evse_dev, site_map=site_map)
    status_sensor.hass = hass
    attrs = status_sensor.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Primary Residence"


async def test_location_attributes_unassigned_site(
    hass: HomeAssistant,
    unassigned_vue_device: VueDevice,
) -> None:
    """Test entities with site_gid=None gracefully report location=None."""
    site_map = {29372: "Primary Residence"}
    coord_usage = MagicMock()
    coord_usage.data = {
        "1002-1,2,3-1MIN": {
            "device_gid": 1002,
            "channel_num": "1,2,3",
            "scale": "1MIN",
            "usage": 1.2,
            "info": unassigned_vue_device,
        }
    }
    power_sensor = CurrentVuePowerSensor(
        coord_usage, "1002-1,2,3-1MIN", site_map=site_map
    )
    power_sensor.hass = hass
    attrs = power_sensor.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] is None


async def test_location_attributes_site_map_change_reflected_dynamically(
    hass: HomeAssistant,
    site_assigned_vue_device: VueDevice,
) -> None:
    """Test that modifying site_map dynamically changes the entity's location attribute."""
    site_map = {29372: "Initial Name"}
    coord_usage = MagicMock()
    coord_usage.data = {
        "1001-1,2,3-1MIN": {
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "scale": "1MIN",
            "usage": 2.5,
            "info": site_assigned_vue_device,
        }
    }
    power_sensor = CurrentVuePowerSensor(
        coord_usage, "1001-1,2,3-1MIN", site_map=site_map
    )
    power_sensor.hass = hass
    assert power_sensor.extra_state_attributes["location"] == "Initial Name"

    # User renames location in Emporia app
    site_map[29372] = "Renamed Estate"
    assert power_sensor.extra_state_attributes["location"] == "Renamed Estate"


async def test_location_attributes_resolved_from_hass_data(
    hass: HomeAssistant,
    site_assigned_vue_device: VueDevice,
) -> None:
    """Test entities dynamically resolve site map from hass.data when not explicitly injected."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_entry_dynamic_site")
    entry.add_to_hass(hass)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "site_map": {29372: "Lake House"},
    }

    coord_usage = MagicMock()
    coord_usage.data = {
        "1001-1,2,3-1MIN": {
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "scale": "1MIN",
            "usage": 2.5,
            "info": site_assigned_vue_device,
        }
    }

    # Notice: site_map parameter omitted
    sensor = CurrentVuePowerSensor(coord_usage, "1001-1,2,3-1MIN")
    sensor.hass = hass

    attrs = sensor.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] == "Lake House"


async def test_location_mixin_null_safety() -> None:
    """Test EmporiaLocationEntityMixin handles device=None and missing attributes safely."""
    mixin = EmporiaLocationEntityMixin()
    mixin._device = None
    mixin._explicit_site_map = {29372: "Home"}

    attrs = mixin.extra_state_attributes
    assert "location_gid" not in attrs
    assert attrs["location"] is None


async def test_async_setup_entry_fetches_and_cross_resolves_sites(
    hass: HomeAssistant,
) -> None:
    """Test async_setup_entry queries vue.get_sites() and populates site_map and device site_gid."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "password"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    vue_dev = VueDevice(gid=1001, modelNum="VUE003")
    vue_dev.device_name = "Main Panel"
    vue_dev.manufacturer_id = "MANUF-1001"
    vue_dev.site_gid = None  # Initially unassigned in devices response
    vue_dev.channels = [
        VueDeviceChannel(
            gid=1001,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        )
    ]
    vue_dev.channels[0].has_data = True

    site = VueSite()
    site.site_gid = 29372
    site.display_name = "Primary Residence"
    site.device_ids = ["MANUF-1001"]

    vue = MagicMock()
    vue.login.return_value = True
    vue.get_devices_v1.return_value = [vue_dev]
    vue.get_sites.return_value = [site]
    vue.get_device_list_usage_v1.return_value = {}

    import custom_components.emporia_vue as ev

    orig_login = ev.async_login_vue
    ev.async_login_vue = AsyncMock(return_value=True)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ev, "PyEmVue", lambda: vue)
            mp.setattr(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(return_value=True),
            )
            # Setup entry
            result = await async_setup_entry(hass, entry)
            assert result is True

            # Verify site_map was populated
            entry_data = hass.data[DOMAIN][entry.entry_id]
            assert "site_map" in entry_data
            assert entry_data["site_map"] == {29372: "Primary Residence"}

            # Verify device cross-resolved site_gid from site.device_ids
            assert vue_dev.site_gid == 29372
    finally:
        ev.async_login_vue = orig_login


async def test_async_setup_entry_handles_get_sites_failure_gracefully(
    hass: HomeAssistant,
) -> None:
    """Test async_setup_entry gracefully defaults site_map to empty dict if get_sites raises."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "password"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    vue_dev = VueDevice(gid=1001, modelNum="VUE003")
    vue_dev.channels = []

    vue = MagicMock()
    vue.login.return_value = True
    vue.get_devices_v1.return_value = [vue_dev]
    vue.get_sites.side_effect = Exception("500 Server Error")
    vue.get_device_list_usage_v1.return_value = {}

    import custom_components.emporia_vue as ev

    orig_login = ev.async_login_vue
    ev.async_login_vue = AsyncMock(return_value=True)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ev, "PyEmVue", lambda: vue)
            mp.setattr(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(return_value=True),
            )
            result = await async_setup_entry(hass, entry)
            assert result is True

            entry_data = hass.data[DOMAIN][entry.entry_id]
            assert entry_data["site_map"] == {}
    finally:
        ev.async_login_vue = orig_login


def test_empty_device_ids_not_falsely_matched() -> None:
    """Devices without manufacturer_id/device_id must not match '' in site.device_ids."""
    vue_dev = VueDevice(gid=1001, modelNum="VUE003")
    vue_dev.manufacturer_id = None
    vue_dev.site_gid = None

    site = VueSite()
    site.site_gid = 29372
    site.display_name = "Primary Residence"
    site.device_ids = ["", None]

    site_map: dict[int, str] = {}
    _populate_site_map_and_assign_devices([site], site_map, {1001: vue_dev})

    assert site_map == {29372: "Primary Residence"}
    assert vue_dev.site_gid is None


async def test_periodic_site_refresh_runs_independently_of_daily_sensors(
    hass: HomeAssistant,
) -> None:
    """Hourly site refresh runs even with ENABLE_1D=False and updates moved devices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "user@example.com",
            "password": "password",
            ENABLE_1D: False,
        },
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    vue_dev = VueDevice(gid=1001, modelNum="VUE003")
    vue_dev.device_name = "Main Panel"
    vue_dev.manufacturer_id = "MANUF-1001"
    vue_dev.site_gid = None
    vue_dev.channels = [
        VueDeviceChannel(
            gid=1001,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        )
    ]
    vue_dev.channels[0].has_data = True

    site1 = VueSite()
    site1.site_gid = 100
    site1.display_name = "Old Home"
    site1.device_ids = ["MANUF-1001"]

    site2 = VueSite()
    site2.site_gid = 200
    site2.display_name = "Cabin"
    site2.device_ids = ["MANUF-1001"]

    vue = MagicMock()
    vue.login.return_value = True
    vue.get_devices_v1.return_value = [vue_dev]
    vue.get_sites.side_effect = [[site1], [site2]]
    vue.get_device_list_usage_v1.return_value = {}

    import custom_components.emporia_vue as ev

    with (
        patch.object(ev, "async_login_vue", AsyncMock(return_value=True)),
        patch.object(ev, "PyEmVue", return_value=vue),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True
        entry_data = hass.data[DOMAIN][entry.entry_id]
        assert entry_data["site_map"] == {100: "Old Home"}
        assert vue_dev.site_gid == 100

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, seconds=1))
        await hass.async_block_till_done()

        assert entry_data["site_map"] == {200: "Cabin"}
        assert vue_dev.site_gid == 200

    # Simulate entry unload cleanup callbacks (canceling interval & shutting down coordinators)
    while entry._on_unload:
        unsub = entry._on_unload.pop()
        result = unsub()
        if inspect.isawaitable(result):
            await result


async def test_multi_entry_site_map_scoping(
    hass: HomeAssistant,
    site_assigned_vue_device: VueDevice,
) -> None:
    """Entities scope hass.data site_map lookups strictly to their owning entry_id."""
    entry1 = MockConfigEntry(domain=DOMAIN, entry_id="entry_1")
    entry2 = MockConfigEntry(domain=DOMAIN, entry_id="entry_2")
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry1.entry_id] = {"site_map": {29372: "Account One Home"}}
    hass.data[DOMAIN][entry2.entry_id] = {"site_map": {29372: "Account Two Cabin"}}

    coord_2 = MagicMock()
    coord_2.config_entry = entry2
    coord_2.data = {
        "1001-1,2,3-1MIN": {
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "scale": "1MIN",
            "usage": 2.5,
            "info": site_assigned_vue_device,
        }
    }

    sensor_2 = CurrentVuePowerSensor(coord_2, "1001-1,2,3-1MIN")
    sensor_2.hass = hass
    assert sensor_2.extra_state_attributes["location"] == "Account Two Cabin"

    # Without entry_id resolution across multiple entries, do not guess entry_1's map
    coord_ambiguous = MagicMock()
    coord_ambiguous.config_entry = None
    coord_ambiguous.data = coord_2.data
    sensor_ambiguous = CurrentVuePowerSensor(coord_ambiguous, "1001-1,2,3-1MIN")
    sensor_ambiguous.hass = hass
    assert sensor_ambiguous.extra_state_attributes["location"] is None
