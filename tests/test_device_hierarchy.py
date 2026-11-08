"""Tests for two-pass via_device hierarchy, circuit models, and guarded dummy CT pruning."""

from unittest.mock import MagicMock, patch

import pytest
from custom_components.emporia_vue import (
    async_prune_stale_devices,
    async_setup_device_registry,
    determine_circuit_model,
)
from custom_components.emporia_vue.charger_entity import EmporiaChargerEntity
from custom_components.emporia_vue.const import (
    DOMAIN,
)
from custom_components.emporia_vue.sensor import CurrentVuePowerSensor
from custom_components.emporia_vue.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.emporia_vue.switch import EmporiaOutletSwitch
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyemvue.device import ChargerDevice, OutletDevice, VueDevice, VueDeviceChannel
from pyemvue.enums import Scale
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]


@pytest.fixture(autouse=True)
def mock_device_translations():
    """Mock backend device translations so device registry resolves translation_key."""
    with patch(
        "homeassistant.helpers.device_registry.translation.async_get_cached_translations",
        return_value={
            "component.emporia_vue.device.circuit.name": "{device_name} circuit {channel_number}"
        },
    ):
        yield


@pytest.fixture
def emporia_devices() -> dict[int, VueDevice]:
    """Return a representative topology of Home, Garage, EVSE, and Outlet devices."""
    home_vue = VueDevice(
        gid=1001,
        modelNum="VUE003",
        firmwareVersion="1.0.0",
    )
    home_vue.device_name = "Home Vue"
    home_vue.manufacturer_id = "MANUF-1001"
    home_vue.channels = [
        VueDeviceChannel(
            gid=1001,
            name=None,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name="Solar Main",
            channelNum="1",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name="Merged Kitchen",
            channelNum="2",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name=None,
            channelNum="3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name=None,
            channelNum="4",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name=None,
            channelNum="Mains_C",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
    ]
    home_vue.channels[0].has_data = True
    home_vue.channels[1].has_data = True
    home_vue.channels[1].channel_classification = "FIFTY_AMP"
    home_vue.channels[2].has_data = True
    home_vue.channels[2].channel_classification = "MERGED"
    home_vue.channels[3].has_data = True
    home_vue.channels[3].channel_classification = ""
    home_vue.channels[4].has_data = False  # unpopulated empty CT jack
    home_vue.channels[5].has_data = False  # split phase Mains_C leg

    garage_vue = VueDevice(
        gid=1002,
        modelNum="VUE002",
        firmwareVersion="1.0.0",
    )
    garage_vue.device_name = "Garage Vue"
    garage_vue.manufacturer_id = "MANUF-1002"
    garage_vue.parent_device_gid = 1001
    garage_vue.channels = [
        VueDeviceChannel(
            gid=1002,
            name=None,
            channelNum="1,2,3",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1002,
            name="Workbench",
            channelNum="1",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
    ]
    garage_vue.channels[0].has_data = True
    garage_vue.channels[1].has_data = True
    garage_vue.channels[1].channel_classification = "FIFTY_AMP"

    ev_charger = VueDevice(
        gid=1003,
        modelNum="EVSE",
        firmwareVersion="2.0.0",
    )
    ev_charger.device_name = "Garage Charger"
    ev_charger.manufacturer_id = "MANUF-1003"
    ev_charger.parent_device_gid = 1002
    ev_charger.channels = []
    ev_charger.ev_charger = ChargerDevice(gid=1003, on=True)
    ev_charger.ev_charger.charging_rate = 32
    ev_charger.ev_charger.max_charging_rate = 48

    smart_outlet = VueDevice(
        gid=1004,
        modelNum="OUTLET",
        firmwareVersion="1.1.0",
    )
    smart_outlet.device_name = "Living Room Outlet"
    smart_outlet.manufacturer_id = "MANUF-1004"
    smart_outlet.parent_device_gid = 1001
    smart_outlet.channels = []
    smart_outlet.outlet = OutletDevice(gid=1004, on=True)

    return {
        1001: home_vue,
        1002: garage_vue,
        1003: ev_charger,
        1004: smart_outlet,
    }


async def test_two_pass_device_hierarchy(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test two-pass hierarchy: subpanels and EVSEs nest under parents, and CTs nest under monitor."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_entry_hierarchy")
    entry.add_to_hass(hass)

    async_setup_device_registry(hass, entry, emporia_devices)

    dev_reg = dr.async_get(hass)

    # 1. Root physical monitor (Home Vue)
    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    assert home_dev is not None
    assert home_dev.name == "Home Vue"
    assert home_dev.model == "VUE003"
    assert home_dev.serial_number == "MANUF-1001"
    assert home_dev.via_device_id is None

    # 2. Child monitor (Garage Vue nested under Home Vue)
    garage_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1002-1,2,3"), entry.entry_id
    )
    assert garage_dev is not None
    assert garage_dev.name == "Garage Vue"
    assert garage_dev.model == "VUE002"
    assert garage_dev.serial_number == "MANUF-1002"
    assert garage_dev.via_device_id == home_dev.id

    # 3. EV Charger (nested under Garage Vue electrical subpanel)
    charger_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1003-1,2,3"), entry.entry_id
    )
    assert charger_dev is not None
    assert charger_dev.name == "Garage Charger"
    assert charger_dev.model == "EVSE"
    assert charger_dev.serial_number == "MANUF-1003"
    assert charger_dev.via_device_id == garage_dev.id

    # 4. Smart Outlet (nested under Home Vue panel)
    outlet_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1004-1,2,3"), entry.entry_id
    )
    assert outlet_dev is not None
    assert outlet_dev.name == "Living Room Outlet"
    assert outlet_dev.serial_number == "MANUF-1004"
    assert outlet_dev.via_device_id == home_dev.id

    # 5. CT Circuit sub-devices nested under Home Vue
    # 50A CT Circuit
    ct1 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
    assert ct1 is not None
    assert ct1.name == "Solar Main"
    assert ct1.model == "50A CT circuit (Branch 1)"
    assert ct1.model_id is None
    assert ct1.via_device_id == home_dev.id

    # Merged CT Circuit
    ct2 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-2"), entry.entry_id)
    assert ct2 is not None
    assert ct2.name == "Merged Kitchen"
    assert ct2.model == "Merged CT circuit (Branch 2)"
    assert ct2.model_id is None
    assert ct2.via_device_id == home_dev.id

    # Active unnamed circuit (#447 fallback naming)
    ct3 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-3"), entry.entry_id)
    assert ct3 is not None
    assert ct3.name == "Home Vue circuit 3"
    assert ct3.model == "50A CT circuit (Branch 3)"
    assert ct3.model_id is None
    assert ct3.via_device_id == home_dev.id

    # Unpopulated / inactive circuits must NOT be pre-registered
    ct4 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-4"), entry.entry_id)
    assert ct4 is None
    ct_mains_c = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-Mains_C"), entry.entry_id
    )
    assert ct_mains_c is None

    # CT sub-device on Garage Vue
    garage_ct1 = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1002-1"), entry.entry_id
    )
    assert garage_ct1 is not None
    assert garage_ct1.name == "Workbench"
    assert garage_ct1.model == "50A CT circuit (Branch 1)"
    assert garage_ct1.model_id is None
    assert garage_ct1.via_device_id == garage_dev.id


async def test_entity_device_info_hierarchy_and_labels(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test DeviceInfo metadata exposed on entities matches hierarchy and descriptive models."""
    home_vue = emporia_devices[1001]
    ev_charger = emporia_devices[1003]
    smart_outlet = emporia_devices[1004]

    # Mock coordinator data structure
    coord_mock = MagicMock()
    coord_mock.data = {
        "1001-1,2,3-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 1500.0,
        },
        "1001-1-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1",
            "info": home_vue,
            "usage": 200.0,
        },
        "1001-3-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "3",
            "info": home_vue,
            "usage": 50.0,
        },
    }

    # 1. Root monitor sensor
    sensor_root = CurrentVuePowerSensor(coord_mock, "1001-1,2,3-1MIN")
    info_root = sensor_root.device_info
    assert info_root["identifiers"] == {(DOMAIN, "1001-1,2,3")}
    assert "via_device" not in info_root
    assert info_root["model"] == "VUE003"
    assert info_root["serial_number"] == "MANUF-1001"

    # 2. 50A branch CT circuit sensor
    sensor_ct1 = CurrentVuePowerSensor(coord_mock, "1001-1-1MIN")
    info_ct1 = sensor_ct1.device_info
    assert info_ct1["identifiers"] == {(DOMAIN, "1001-1")}
    assert "via_device" not in info_ct1
    assert info_ct1["name"] == "Solar Main"
    assert info_ct1["model"] == "50A CT circuit (Branch 1)"
    assert info_ct1.get("model_id") is None

    # 3. Unnamed active circuit sensor (#447 fallback naming preserved)
    sensor_ct3 = CurrentVuePowerSensor(coord_mock, "1001-3-1MIN")
    info_ct3 = sensor_ct3.device_info
    assert info_ct3["identifiers"] == {(DOMAIN, "1001-3")}
    assert "via_device" not in info_ct3
    assert info_ct3["name"] == "Home Vue circuit 3"
    assert info_ct3["model"] == "50A CT circuit (Branch 3)"
    assert info_ct3.get("model_id") is None
    assert info_ct3["translation_key"] == "circuit"
    assert info_ct3["translation_placeholders"] == {
        "device_name": "Home Vue",
        "channel_number": "3",
    }

    # 4. EV Charger entity
    status_coord = MagicMock()
    status_coord.data = {"1003": ev_charger.ev_charger}
    charger_entity = EmporiaChargerEntity(
        status_coord, MagicMock(), ev_charger, None, "outlet"
    )
    info_charger = charger_entity.device_info
    assert info_charger["identifiers"] == {(DOMAIN, "1003-1,2,3")}
    assert "via_device" not in info_charger
    assert info_charger["name"] == "Garage Charger"
    assert info_charger["serial_number"] == "MANUF-1003"

    # 5. Smart Outlet switch entity
    outlet_coord = MagicMock()
    outlet_coord.data = {"1004": smart_outlet.outlet}
    outlet_entity = EmporiaOutletSwitch(outlet_coord, MagicMock(), "1004", smart_outlet)
    info_outlet = outlet_entity.device_info
    assert info_outlet["identifiers"] == {(DOMAIN, "1004-1,2,3")}
    assert "via_device" not in info_outlet
    assert info_outlet["name"] == "Living Room Outlet"
    assert info_outlet["serial_number"] == "MANUF-1004"


async def test_has_data_false_channel_entity_suppression(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test that channels with has_data=False are suppressed in sensor platform setup."""
    home_vue = emporia_devices[1001]
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_entry_suppress")
    entry.add_to_hass(hass)

    coord_mock = MagicMock()
    coord_mock.data = {
        "1001-1,2,3-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 1500.0,
        },
        "1001-1-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1",
            "info": home_vue,
            "usage": 200.0,
        },
        "1001-4-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "4",
            "info": home_vue,
            "usage": 0.0,
        },
        "1001-Mains_C-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Mains_C",
            "info": home_vue,
            "usage": 0.0,
        },
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_1min": coord_mock,
        "coordinator_1mon": None,
        "coordinator_day_sensor": None,
        "coordinator_device_status": None,
        "retry_update_methods": {},
        "device_information": emporia_devices,
    }

    added_entities = []

    def mock_add_entities(entities) -> None:
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, mock_add_entities)

    channel_nums = [e._channel.channel_num for e in added_entities]
    assert "1,2,3" in channel_nums
    assert "1" in channel_nums
    assert "4" not in channel_nums  # suppressed because has_data is False
    assert "Mains_C" not in channel_nums  # suppressed because has_data is False


async def test_safe_device_pruning(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test safe device pruning: dummy uncustomized CT devices are removed, customized are preserved."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_entry_pruning")
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # Device 1: inactive dummy CT device (channel 4, has_data=False), NO customizations -> MUST BE PRUNED
    d1 = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-4")},
        name="Home Vue circuit 4",
    )
    ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="sensor.emporia_vue.instant.1001-4",
        config_entry=entry,
        device_id=d1.id,
    )

    # Device 2: inactive dummy CT device (channel 5), customized name_by_user -> PRESERVED
    ch5 = VueDeviceChannel(
        gid=1001, name=None, channelNum="5", channelMultiplier=1.0, channelTypeGid=1
    )
    ch5.has_data = False
    emporia_devices[1001].channels.append(ch5)

    d2 = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-5")},
        name="Home Vue circuit 5",
    )
    dev_reg.async_update_device(d2.id, name_by_user="User Renamed Circuit 5")

    # Device 3: inactive dummy CT device (channel 6), customized area_id -> PRESERVED
    ch6 = VueDeviceChannel(
        gid=1001, name=None, channelNum="6", channelMultiplier=1.0, channelTypeGid=1
    )
    ch6.has_data = False
    emporia_devices[1001].channels.append(ch6)

    d3 = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-6")},
        name="Home Vue circuit 6",
    )
    dev_reg.async_update_device(d3.id, area_id="kitchen_area")

    # Device 4: inactive dummy CT device (channel 7), customized labels -> PRESERVED
    ch7 = VueDeviceChannel(
        gid=1001, name=None, channelNum="7", channelMultiplier=1.0, channelTypeGid=1
    )
    ch7.has_data = False
    emporia_devices[1001].channels.append(ch7)

    d4 = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-7")},
        name="Home Vue circuit 7",
    )
    dev_reg.async_update_device(d4.id, labels={"favorite"})

    # Device 5: inactive dummy CT device (channel 8), has entity disabled by USER -> PRESERVED
    ch8 = VueDeviceChannel(
        gid=1001, name=None, channelNum="8", channelMultiplier=1.0, channelTypeGid=1
    )
    ch8.has_data = False
    emporia_devices[1001].channels.append(ch8)

    d5 = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-8")},
        name="Home Vue circuit 8",
    )
    ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="sensor.emporia_vue.instant.1001-8",
        config_entry=entry,
        device_id=d5.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    # Device 6: active CT device (channel 1, has_data=True), NO customizations -> PRESERVED
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-1")},
        name="Solar Main",
    )

    # Device 7: physical monitor device (1001-1,2,3), NO customizations -> PRESERVED
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-1,2,3")},
        name="Home Vue",
    )

    # Run pruning
    async_prune_stale_devices(hass, entry, emporia_devices)

    # Verify d1 (dummy uncustomized) was pruned along with its entities
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-4"), entry.entry_id)
        is None
    )
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, "sensor.emporia_vue.instant.1001-4"
        )
        is None
    )

    # Verify d2, d3, d4, d5, d6, d7 were all preserved
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-5"), entry.entry_id)
        is not None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-6"), entry.entry_id)
        is not None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-7"), entry.entry_id)
        is not None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-8"), entry.entry_id)
        is not None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
        is not None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1,2,3"), entry.entry_id)
        is not None
    )


async def test_determine_circuit_model_classifications() -> None:
    """Test model assignment mapping for various CT channel types."""
    ch_50a = VueDeviceChannel(gid=1, channelNum="1")
    ch_50a.channel_classification = "FIFTY_AMP"
    assert determine_circuit_model(ch_50a) == "50A CT circuit (Branch 1)"

    ch_legacy_50a = VueDeviceChannel(gid=1, channelNum="2")
    ch_legacy_50a.type = "FiftyAmp"
    assert determine_circuit_model(ch_legacy_50a) == "50A CT circuit (Branch 2)"

    ch_bidi = VueDeviceChannel(gid=1, channelNum="3")
    ch_bidi.type = "FiftyAmpBidirectional"
    assert determine_circuit_model(ch_bidi) == "50A CT circuit (Branch 3)"

    ch_merged = VueDeviceChannel(gid=1, channelNum="4")
    ch_merged.channel_classification = "MERGED"
    assert determine_circuit_model(ch_merged) == "Merged CT circuit (Branch 4)"

    ch_main = VueDeviceChannel(gid=1, channelNum="5")
    ch_main.channel_classification = "MAIN"
    assert determine_circuit_model(ch_main) == "Main CT circuit"

    ch_200a = VueDeviceChannel(gid=1, channelNum="6")
    ch_200a.channel_classification = "TWO_HUNDRED_AMP"
    assert determine_circuit_model(ch_200a) == "200A CT circuit"

    ch_default = VueDeviceChannel(gid=1, channelNum="7")
    assert determine_circuit_model(ch_default) == "50A CT circuit (Branch 7)"

    # Emporia cloud synthetic virtual channels (non-CTs)
    ch_balance = VueDeviceChannel(gid=1, channelNum="Balance")
    assert determine_circuit_model(ch_balance) == "Virtual circuit (Balance)"

    ch_total = VueDeviceChannel(gid=1, channelNum="TotalUsage")
    assert determine_circuit_model(ch_total) == "Virtual circuit (Total usage)"

    ch_grid_in = VueDeviceChannel(gid=1, channelNum="MainsFromGrid")
    assert determine_circuit_model(ch_grid_in) == "Virtual circuit (Mains from grid)"

    ch_grid_out = VueDeviceChannel(gid=1, channelNum="MainsToGrid")
    assert determine_circuit_model(ch_grid_out) == "Virtual circuit (Mains to grid)"




async def test_device_info_name_fallback_when_device_name_empty(
    hass: HomeAssistant,
) -> None:
    """Assert DeviceInfo.name cleanly falls back to display_name or model when device_name is empty."""
    # 1. Fallback to display_name for main monitor
    main_ch = VueDeviceChannel()
    main_ch.channel_num = "1,2,3"
    main_ch.name = "Main"
    main_ch.device_gid = 9999

    dev_with_display = VueDevice()
    dev_with_display.device_gid = 9999
    dev_with_display.device_name = ""
    dev_with_display.display_name = "Custom Panel Display"
    dev_with_display.model = "VUE003"
    dev_with_display.firmware = "Vue3-100"
    dev_with_display.channels = [main_ch]

    ident_key = "9999-1,2,3-1MIN"
    coord = MagicMock()
    coord.data = {
        ident_key: {
            "scale": Scale.MINUTE.value,
            "device_gid": 9999,
            "channel_num": "1,2,3",
            "info": dev_with_display,
            "usage": 50.0,
        }
    }
    sensor1 = CurrentVuePowerSensor(coord, ident_key)
    assert sensor1.device_info["name"] == "Custom Panel Display"

    # 2. Fallback to model when both device_name and display_name are empty
    dev_with_model_only = VueDevice()
    dev_with_model_only.device_gid = 9999
    dev_with_model_only.device_name = ""
    dev_with_model_only.display_name = ""
    dev_with_model_only.model = "VUE003"
    dev_with_model_only.firmware = "Vue3-100"
    dev_with_model_only.channels = [main_ch]

    coord.data[ident_key]["info"] = dev_with_model_only
    sensor2 = CurrentVuePowerSensor(coord, ident_key)
    assert sensor2.device_info["name"] == "VUE003"

    # 3. Unnamed circuit channel fallback using monitor fallback name
    ct_channel = VueDeviceChannel()
    ct_channel.channel_num = "1"
    ct_channel.name = ""
    ct_channel.device_gid = 9999
    dev_with_display.channels = [main_ch, ct_channel]

    ct_key = "9999-1-1MIN"
    coord.data[ct_key] = {
        "scale": Scale.MINUTE.value,
        "device_gid": 9999,
        "channel_num": "1",
        "info": dev_with_display,
        "usage": 20.0,
    }
    sensor_ct = CurrentVuePowerSensor(coord, ct_key)
    assert sensor_ct.device_info["name"] == "Custom Panel Display circuit 1"
    assert sensor_ct.device_info["translation_key"] == "circuit"
    assert sensor_ct.device_info["translation_placeholders"] == {
        "device_name": "Custom Panel Display",
        "channel_number": "1",
    }


async def test_cascading_device_disable(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test cascading parent monitor disable to child hardware, CT circuits, and virtual circuits."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_cascade_entry")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    # 1. Initial setup - all devices enabled
    async_setup_device_registry(hass, entry, emporia_devices)

    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    garage_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1002-1,2,3"), entry.entry_id
    )
    ct1 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
    ct3 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-3"), entry.entry_id)

    # Also register a virtual circuit (e.g. Balance) linked via via_device_id
    balance_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-Balance")},
        name="Home Vue Balance",
        via_device_id=home_dev.id,
    )

    assert home_dev is not None and home_dev.disabled_by is None
    assert garage_dev is not None and garage_dev.disabled_by is None
    assert ct1 is not None and ct1.disabled_by is None
    assert ct3 is not None and ct3.disabled_by is None
    assert balance_dev is not None and balance_dev.disabled_by is None

    # 2. Manually disable circuit 3 as a USER
    dev_reg.async_update_device(ct3.id, disabled_by=dr.DeviceEntryDisabler.USER)

    # 3. Disable parent monitor (Home Vue)
    dev_reg.async_update_device(home_dev.id, disabled_by=dr.DeviceEntryDisabler.USER)

    # 4. Trigger registry setup (as on integration reload)
    async_setup_device_registry(hass, entry, emporia_devices)

    # Re-fetch device states
    home_dev = dev_reg.async_get(home_dev.id)
    garage_dev = dev_reg.async_get(garage_dev.id)
    ct1 = dev_reg.async_get(ct1.id)
    ct3 = dev_reg.async_get(ct3.id)
    balance_dev = dev_reg.async_get(balance_dev.id)

    assert home_dev.disabled_by == dr.DeviceEntryDisabler.USER
    # Child devices must have cascaded INTEGRATION disable
    assert garage_dev.disabled_by == dr.DeviceEntryDisabler.INTEGRATION
    assert ct1.disabled_by == dr.DeviceEntryDisabler.INTEGRATION
    assert balance_dev.disabled_by == dr.DeviceEntryDisabler.INTEGRATION
    # Manually disabled circuit must retain USER disable
    assert ct3.disabled_by == dr.DeviceEntryDisabler.USER

    # 5. Re-enable parent monitor
    dev_reg.async_update_device(home_dev.id, disabled_by=None)

    # 6. Trigger registry setup on reload
    async_setup_device_registry(hass, entry, emporia_devices)

    # Re-fetch device states
    home_dev = dev_reg.async_get(home_dev.id)
    garage_dev = dev_reg.async_get(garage_dev.id)
    ct1 = dev_reg.async_get(ct1.id)
    ct3 = dev_reg.async_get(ct3.id)
    balance_dev = dev_reg.async_get(balance_dev.id)

    assert home_dev.disabled_by is None
    # Cascaded devices must be restored to enabled
    assert garage_dev.disabled_by is None
    assert ct1.disabled_by is None
    assert balance_dev.disabled_by is None
    # Manually disabled circuit must remain disabled by USER
    assert ct3.disabled_by == dr.DeviceEntryDisabler.USER


async def test_realtime_cascading_device_disable_event(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test real-time event-driven cascading disable without integration reload."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_realtime_cascade_entry")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    async_setup_device_registry(hass, entry, emporia_devices)

    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    garage_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1002-1,2,3"), entry.entry_id
    )
    ct1 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
    ct3 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-3"), entry.entry_id)

    from custom_components.emporia_vue import sync_device_disabled_cascade
    from homeassistant.core import callback

    @callback
    def _async_handle_device_registry_update(
        event: Event[dr.EventDeviceRegistryUpdatedData],
    ) -> None:
        if event.data.get("action") != "update":
            return
        changes = event.data.get("changes", {})
        if "disabled_by" not in changes:
            return
        device_id = event.data.get("device_id")
        updated_dev = dev_reg.async_get(device_id)
        dev_entry_id = getattr(updated_dev, "config_entry_id", None)
        dev_entries = getattr(updated_dev, "config_entries", set())
        if not updated_dev or (
            dev_entry_id != entry.entry_id and entry.entry_id not in dev_entries
        ):
            return
        sync_device_disabled_cascade(
            dev_reg,
            entry.entry_id,
            updated_dev.id,
            bool(updated_dev.disabled_by),
        )

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, _async_handle_device_registry_update
        )
    )

    # 1. Manually disable circuit 3 by USER
    dev_reg.async_update_device(ct3.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()

    # 2. Disable Home Vue via UI/dev_reg in real time
    dev_reg.async_update_device(home_dev.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()

    # Child devices must be cascaded without reload
    garage_dev = dev_reg.async_get(garage_dev.id)
    ct1 = dev_reg.async_get(ct1.id)
    ct3 = dev_reg.async_get(ct3.id)

    assert garage_dev.disabled_by == dr.DeviceEntryDisabler.INTEGRATION
    assert ct1.disabled_by == dr.DeviceEntryDisabler.INTEGRATION
    assert ct3.disabled_by == dr.DeviceEntryDisabler.USER

    # 3. Re-enable Home Vue via UI/dev_reg in real time
    dev_reg.async_update_device(home_dev.id, disabled_by=None)
    await hass.async_block_till_done()

    garage_dev = dev_reg.async_get(garage_dev.id)
    ct1 = dev_reg.async_get(ct1.id)
    ct3 = dev_reg.async_get(ct3.id)

    assert garage_dev.disabled_by is None
    assert ct1.disabled_by is None
    assert ct3.disabled_by == dr.DeviceEntryDisabler.USER


async def test_pass_2b_links_existing_unused_cts_via_device_id(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test that existing unused CT sub-devices without via_device_id are linked to their parent monitor."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_unlinked_cts_entry")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    # Pre-create an unused CT device without via_device_id (e.g. from legacy integration version)
    unused_ct = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-4")},
        name="Garage Circuit 4",
    )
    dev_reg.async_update_device(unused_ct.id, name_by_user="x4 Garage")
    assert unused_ct.via_device_id is None

    # Run setup
    async_setup_device_registry(hass, entry, emporia_devices)

    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    updated_ct = dev_reg.async_get(unused_ct.id)

    # via_device_id must now be linked to parent monitor
    assert updated_ct.via_device_id == home_dev.id
    # User customization must be strictly preserved
    assert updated_ct.name_by_user == "x4 Garage"
