"""Tests for two-pass via_device hierarchy, circuit models, and guarded dummy CT pruning."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyemvue.device import ChargerDevice, OutletDevice, VueDevice, VueDeviceChannel
from pyemvue.enums import Scale
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue import (
    _resolve_parent_device_gids,
    async_prune_stale_devices,
    async_register_device_registry_listener,
    async_remove_config_entry_device,
    async_setup_device_registry,
    determine_circuit_model,
)
from custom_components.emporia_vue.charger_entity import EmporiaChargerEntity
from custom_components.emporia_vue.const import (
    DOMAIN,
    VUE_DATA,
)
from custom_components.emporia_vue.sensor import CurrentVuePowerSensor
from custom_components.emporia_vue.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.emporia_vue.switch import (
    EmporiaChargerSwitch,
    EmporiaOutletSwitch,
)
from custom_components.emporia_vue.switch import (
    async_setup_entry as switch_async_setup_entry,
)

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


async def test_device_registry_unnamed_device_fallback_and_parent_id_resolution(
    hass: HomeAssistant,
) -> None:
    """Unnamed hardware falls back cleanly in Pass 1/Pass 2 and resolves manufacturer_id parents."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    parent = VueDevice(gid=5001)
    parent.device_name = ""
    parent.display_name = ""
    parent.model = "VUE003"
    parent.device_id = "UUID-5001"
    parent.manufacturer_id = "MANUF-5001"
    ch = VueDeviceChannel(gid=5001, channelNum="1")
    ch.name = ""
    ch.has_data = True
    parent.channels = [ch]

    child = VueDevice(gid=5002)
    child.device_name = ""
    child.display_name = ""
    child.model = ""
    child.parent_device_gid = 0
    child.parent_device_id = (
        "MANUF-5001"  # matches manufacturer_id instead of device_id
    )
    child.channels = []

    dev_map = {5001: parent, 5002: child}
    _resolve_parent_device_gids(dev_map)
    assert child.parent_device_gid == 5001

    async_setup_device_registry(hass, entry, dev_map)
    dev_reg = dr.async_get(hass)

    parent_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "5001-1,2,3"), entry.entry_id
    )
    assert parent_entry is not None
    assert parent_entry.name == "VUE003"

    circuit_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "5001-1"), entry.entry_id
    )
    assert circuit_entry is not None
    assert circuit_entry.name == "VUE003 circuit 1"

    child_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "5002-1,2,3"), entry.entry_id
    )
    assert child_entry is not None
    assert child_entry.name == "Vue Device 5002"
    assert child_entry.via_device_id == parent_entry.id


async def test_prune_preserves_devices_with_entity_customizations(
    hass: HomeAssistant,
) -> None:
    """Stale devices with user-hidden/named/icon/area/label entities must not be pruned."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    test_cases = [
        ("1", {"hidden_by": er.RegistryEntryHider.USER}),
        ("2", {"name": "Custom Entity Name"}),
        ("3", {"icon": "mdi:flash"}),
        ("4", {"area_id": "garage"}),
        ("5", {"labels": {"important"}}),
    ]
    channels: list[VueDeviceChannel] = []
    for idx, (ch_num, ent_kwargs) in enumerate(test_cases, start=1):
        ch = VueDeviceChannel(gid=6001, channelNum=ch_num)
        ch.has_data = False
        channels.append(ch)
        dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"6001-{ch_num}")},
            name=f"Dummy {ch_num}",
        )
        ent = ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"sensor.emporia_vue.6001.{ch_num}.1MIN",
            config_entry=entry,
            device_id=dev.id,
        )
        ent_reg.async_update_entity(ent.entity_id, **ent_kwargs)

    mon = VueDevice(gid=6001)
    mon.channels = channels
    async_prune_stale_devices(hass, entry, {6001: mon})

    for ch_num, _ in test_cases:
        assert (
            dev_reg.async_get_device_by_identifier(
                (DOMAIN, f"6001-{ch_num}"), entry.entry_id
            )
            is not None
        )


async def test_switches_registered_when_channels_have_has_data_false(
    hass: HomeAssistant,
) -> None:
    """Active smart outlets and EVSEs must keep switches even if all channels report has_data=False."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "test@example.com"})
    entry.add_to_hass(hass)

    outlet_dev = VueDevice(gid=7001)
    outlet_ch = VueDeviceChannel(gid=7001, channelNum="1,2,3")
    outlet_ch.has_data = False
    outlet_dev.channels = [outlet_ch]
    outlet_dev.outlet = OutletDevice(gid=7001, on=True)

    charger_dev = VueDevice(gid=7002)
    charger_ch = VueDeviceChannel(gid=7002, channelNum="1,2,3")
    charger_ch.has_data = False
    charger_dev.channels = [charger_ch]
    charger_dev.ev_charger = ChargerDevice(gid=7002, on=False)

    coord = MagicMock()
    coord.data = {"7001": outlet_dev.outlet, "7002": charger_dev.ev_charger}

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: MagicMock(),
        "coordinator_device_status": coord,
        "device_information": {7001: outlet_dev, 7002: charger_dev},
    }

    added: list = []
    await switch_async_setup_entry(hass, entry, added.extend)

    assert len(added) == 2
    assert any(isinstance(e, EmporiaOutletSwitch) for e in added)
    assert any(isinstance(e, EmporiaChargerSwitch) for e in added)


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

    async_register_device_registry_listener(hass, entry)

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

    # 2b. Manually re-enabling a child while parent remains disabled must re-apply INTEGRATION disable
    dev_reg.async_update_device(ct1.id, disabled_by=None)
    await hass.async_block_till_done()
    ct1 = dev_reg.async_get(ct1.id)
    assert ct1.disabled_by == dr.DeviceEntryDisabler.INTEGRATION

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
    """Test that existing unused CT sub-devices without via_device_id are linked, while mains legs are excluded."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_unlinked_cts_entry")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    # Pre-create an unused CT device and a Mains_A device without via_device_id
    unused_ct = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-4")},
        name="Garage Circuit 4",
    )
    dev_reg.async_update_device(unused_ct.id, name_by_user="x4 Garage")
    assert unused_ct.via_device_id is None

    mains_a = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-Mains_A")},
        name="Legacy Mains_A",
    )
    assert mains_a.via_device_id is None

    # Run setup
    async_setup_device_registry(hass, entry, emporia_devices)

    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    updated_ct = dev_reg.async_get(unused_ct.id)
    updated_mains_a = dev_reg.async_get(mains_a.id)

    # via_device_id must now be linked to parent monitor
    assert updated_ct.via_device_id == home_dev.id
    # User customization must be strictly preserved
    assert updated_ct.name_by_user == "x4 Garage"
    # Mains_A must not be adopted by Pass 2b
    assert updated_mains_a.via_device_id is None


async def test_async_remove_config_entry_device(
    hass: HomeAssistant, emporia_devices: dict[int, VueDevice]
) -> None:
    """Test async_remove_config_entry_device allows deleting inactive/orphaned devices and protects active ones."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_remove_device_entry")
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)

    # 1001-1 is channels[1] (has_data=True), 1001-4 is channels[4] (has_data=False)
    assert emporia_devices[1001].channels[1].channel_num == "1"
    assert emporia_devices[1001].channels[4].channel_num == "4"
    emporia_devices[1001].channels[1].has_data = True
    emporia_devices[1001].channels[4].has_data = False

    # 0. Unloaded entry (missing hass.data entry): MUST reject deletion cleanly
    monitor_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-1,2,3")},
    )
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await async_remove_config_entry_device(hass, entry, monitor_dev)

    coord_1min = MagicMock()
    coord_1min.data = {
        "1001-Balance-1MIN": {"device_gid": 1001, "channel_num": "Balance"}
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "device_information": emporia_devices,
        "coordinator_1min": coord_1min,
    }

    # 1. Main panel monitor device: MUST NOT be removable (raises HomeAssistantError)
    with pytest.raises(
        HomeAssistantError,
        match="Cannot remove active device '.*'. Only inactive or decommissioned devices can be deleted.",
    ):
        await async_remove_config_entry_device(hass, entry, monitor_dev)

    # 2. Active circuit device: MUST NOT be removable (raises HomeAssistantError)
    active_ct_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-1")},
    )
    with pytest.raises(
        HomeAssistantError,
        match="Cannot remove active device '.*'. Only inactive or decommissioned devices can be deleted.",
    ):
        await async_remove_config_entry_device(hass, entry, active_ct_dev)

    # 2b. Virtual coordinator channel (e.g. 1001-Balance): MUST NOT be removable
    balance_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-Balance")},
    )
    with pytest.raises(
        HomeAssistantError,
        match="Cannot remove active device '.*'. Only inactive or decommissioned devices can be deleted.",
    ):
        await async_remove_config_entry_device(hass, entry, balance_dev)

    # 3. Inactive circuit device (has_data=False): MUST be removable
    inactive_ct_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-4")},
    )
    assert await async_remove_config_entry_device(hass, entry, inactive_ct_dev) is True

    # 4. Orphaned legacy circuit device (e.g. 1001-97, not in cloud): MUST be removable
    orphaned_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1001-97")},
    )
    assert await async_remove_config_entry_device(hass, entry, orphaned_dev) is True
