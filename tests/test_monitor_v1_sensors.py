"""Tests for Split-Phase Mains A/B Voltage & Power Sensors & Merged Circuits."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfElectricPotential, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pyemvue.device import (
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale, Unit
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue import (
    async_setup_device_registry,
    determine_circuit_model,
    update_sensors,
)
from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.sensor import (
    CurrentVuePowerSensor,
    CurrentVueVoltageSensor,
)
from custom_components.emporia_vue.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)


@pytest.fixture
def split_phase_devices() -> dict[int, VueDevice]:
    """Return Vue devices including split-phase mains legs and a merged circuit."""
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
            name=None,
            channelNum="Mains_A",
            channelMultiplier=1.0,
            channelTypeGid=1,
        ),
        VueDeviceChannel(
            gid=1001,
            name=None,
            channelNum="Mains_B",
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
        VueDeviceChannel(
            gid=1001,
            name="Main floor",
            channelNum="Merged_100",
            channelMultiplier=1.0,
            channelTypeGid=21,
        ),
    ]
    home_vue.channels[0].has_data = True  # Aggregate Mains 1,2,3
    home_vue.channels[1].has_data = True  # Split-phase Leg A
    home_vue.channels[2].has_data = True  # Split-phase Leg B
    home_vue.channels[
        3
    ].has_data = False  # Split-phase Leg C (inactive in 120/240V split-phase)
    home_vue.channels[4].has_data = True  # Merged virtual circuit
    home_vue.channels[4].channel_classification = "MERGED"

    return {1001: home_vue}


async def test_split_phase_mains_power_and_voltage_sensors(
    hass: HomeAssistant, split_phase_devices: dict[int, VueDevice]
) -> None:
    """Test instantaneous power and voltage sensor creation for Mains A and Mains B legs."""
    home_vue = split_phase_devices[1001]
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_split_phase_sensors")
    entry.add_to_hass(hass)

    coord_1min = MagicMock()
    coord_1min.data = {
        "1001-1,2,3-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 0.0020296568,
            "voltage": 120.75,
        },
        "1001-Mains_A-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Mains_A",
            "info": home_vue,
            "usage": 0.001015,
            "voltage": 120.4,
        },
        "1001-Mains_B-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Mains_B",
            "info": home_vue,
            "usage": 0.0010146568,
            "voltage": 121.1,
        },
        "1001-Mains_C-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Mains_C",
            "info": home_vue,
            "usage": 0.0,
            "voltage": 0.0,
        },
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_1min": coord_1min,
        "coordinator_1mon": None,
        "coordinator_day_sensor": None,
        "coordinator_device_status": None,
        "retry_update_methods": {},
        "device_information": split_phase_devices,
    }

    added_entities = []

    def mock_add_entities(entities) -> None:
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, mock_add_entities)

    power_sensors = [e for e in added_entities if isinstance(e, CurrentVuePowerSensor)]
    voltage_sensors = [
        e for e in added_entities if isinstance(e, CurrentVueVoltageSensor)
    ]

    # Verify split-phase Mains A and Mains B do NOT create phantom power sensors
    assert not any(e._channel.channel_num == "Mains_A" for e in power_sensors)
    assert not any(e._channel.channel_num == "Mains_B" for e in power_sensors)
    assert not any(e._channel.channel_num == "Mains_C" for e in power_sensors)

    # Verify aggregate mains 1,2,3 power sensor is created
    aggregate_power = next(
        (e for e in power_sensors if e._channel.channel_num == "1,2,3"), None
    )
    assert aggregate_power is not None
    assert aggregate_power.name == "Power Minute Average"

    # Verify Mains A Voltage Sensor
    mains_a_voltage = next(
        (e for e in voltage_sensors if e._channel.channel_num == "Mains_A"), None
    )
    assert mains_a_voltage is not None
    assert mains_a_voltage.name == "Mains A voltage minute average"
    assert mains_a_voltage.translation_key == "mains_voltage_minute_average"
    assert mains_a_voltage.translation_placeholders == {"leg": "A"}
    assert mains_a_voltage.device_class == SensorDeviceClass.VOLTAGE
    assert mains_a_voltage.native_unit_of_measurement == UnitOfElectricPotential.VOLT
    assert mains_a_voltage.state_class == SensorStateClass.MEASUREMENT
    assert mains_a_voltage.unique_id == "sensor.emporia_vue.voltage.1001-Mains_A"
    assert mains_a_voltage.native_value == pytest.approx(120.4)
    assert mains_a_voltage.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}

    # Verify Mains B Voltage Sensor
    mains_b_voltage = next(
        (e for e in voltage_sensors if e._channel.channel_num == "Mains_B"), None
    )
    assert mains_b_voltage is not None
    assert mains_b_voltage.name == "Mains B voltage minute average"
    assert mains_b_voltage.translation_key == "mains_voltage_minute_average"
    assert mains_b_voltage.translation_placeholders == {"leg": "B"}
    assert mains_b_voltage.device_class == SensorDeviceClass.VOLTAGE
    assert mains_b_voltage.native_unit_of_measurement == UnitOfElectricPotential.VOLT
    assert mains_b_voltage.state_class == SensorStateClass.MEASUREMENT
    assert mains_b_voltage.unique_id == "sensor.emporia_vue.voltage.1001-Mains_B"
    assert mains_b_voltage.native_value == pytest.approx(121.1)
    assert mains_b_voltage.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}

    # Verify Mains C voltage sensor suppressed
    assert not any(e._channel.channel_num == "Mains_C" for e in voltage_sensors)


async def test_merged_virtual_circuit_device_and_sensors(
    hass: HomeAssistant, split_phase_devices: dict[int, VueDevice]
) -> None:
    """Test Merged_100 virtual circuit sub-device and sensor hierarchy."""
    home_vue = split_phase_devices[1001]
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_merged_circuit")
    entry.add_to_hass(hass)

    # 1. Device Registry pre-registration
    async_setup_device_registry(hass, entry, split_phase_devices)
    dev_reg = dr.async_get(hass)

    home_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    assert home_dev is not None

    merged_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-Merged_100"), entry.entry_id
    )
    assert merged_dev is not None
    assert merged_dev.name == "Main floor"
    assert merged_dev.model == "Merged virtual circuit"
    assert merged_dev.via_device_id == home_dev.id

    # Mains legs must NOT be registered as separate CT circuit sub-devices
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-Mains_A"), entry.entry_id)
        is None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-Mains_B"), entry.entry_id)
        is None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, "1001-Mains_C"), entry.entry_id)
        is None
    )

    # 2. Sensors setup for Merged_100 across 1min, day, and month coordinators
    coord_1min = MagicMock()
    coord_1min.data = {
        "1001-Merged_100-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Merged_100",
            "info": home_vue,
            "usage": 0.0003,
            "voltage": None,
        }
    }
    coord_day = MagicMock()
    coord_day.data = {
        "1001-Merged_100-1D": {
            "scale": "1D",
            "device_gid": 1001,
            "channel_num": "Merged_100",
            "info": home_vue,
            "usage": 1.5,
            "reset": datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        }
    }
    coord_mon = MagicMock()
    coord_mon.data = {
        "1001-Merged_100-1MON": {
            "scale": "1MON",
            "device_gid": 1001,
            "channel_num": "Merged_100",
            "info": home_vue,
            "usage": 45.0,
            "reset": datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC),
        }
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_1min": coord_1min,
        "coordinator_1mon": coord_mon,
        "coordinator_day_sensor": coord_day,
        "coordinator_device_status": None,
        "retry_update_methods": {},
        "device_information": split_phase_devices,
    }

    added_entities = []

    def mock_add_entities(entities) -> None:
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, mock_add_entities)

    # 1MIN Power Sensor for Merged Circuit
    merged_power = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "Merged_100" and e._scale == "1MIN"
        ),
        None,
    )
    assert merged_power is not None
    assert merged_power.name == "Power Minute Average"
    assert merged_power.unique_id == "sensor.emporia_vue.instant.1001-Merged_100"
    assert merged_power.native_value == pytest.approx(0.0003 * 60 * 1000)
    assert merged_power.device_info["identifiers"] == {(DOMAIN, "1001-Merged_100")}
    assert "via_device" not in merged_power.device_info
    assert merged_power.device_info["model"] == "Merged virtual circuit"
    assert merged_power.device_info["name"] == "Main floor"

    # 1D Energy Sensor for Merged Circuit
    merged_day = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "Merged_100" and e._scale == "1D"
        ),
        None,
    )
    assert merged_day is not None
    assert merged_day.name == "Energy Today"
    assert merged_day.device_class == SensorDeviceClass.ENERGY
    assert merged_day.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert merged_day.unique_id == "sensor.emporia_vue.1D.1001-Merged_100"
    assert merged_day.native_value == pytest.approx(1.5)
    assert merged_day.device_info["identifiers"] == {(DOMAIN, "1001-Merged_100")}
    assert merged_day.device_info["model"] == "Merged virtual circuit"

    # 1MON Energy Sensor for Merged Circuit
    merged_mon = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "Merged_100" and e._scale == "1MON"
        ),
        None,
    )
    assert merged_mon is not None
    assert merged_mon.name == "Energy This Month"
    assert merged_mon.device_class == SensorDeviceClass.ENERGY
    assert merged_mon.unique_id == "sensor.emporia_vue.1MON.1001-Merged_100"
    assert merged_mon.native_value == pytest.approx(45.0)


async def test_split_phase_mains_backward_compatibility(
    hass: HomeAssistant, split_phase_devices: dict[int, VueDevice]
) -> None:
    """Assert aggregate mains 1,2,3 sensors exist and match existing behavior."""
    home_vue = split_phase_devices[1001]
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_backward_compat")
    entry.add_to_hass(hass)

    coord_1min = MagicMock()
    coord_1min.data = {
        "1001-1,2,3-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 2.5,
        },
        "1001-Mains_A-1MIN": {
            "scale": "1MIN",
            "device_gid": 1001,
            "channel_num": "Mains_A",
            "info": home_vue,
            "usage": 1.25,
        },
    }
    coord_day = MagicMock()
    coord_day.data = {
        "1001-1,2,3-1D": {
            "scale": "1D",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 25.0,
            "reset": datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        },
        "1001-Mains_A-1D": {
            "scale": "1D",
            "device_gid": 1001,
            "channel_num": "Mains_A",
            "info": home_vue,
            "usage": 12.5,
            "reset": datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        },
    }
    coord_mon = MagicMock()
    coord_mon.data = {
        "1001-1,2,3-1MON": {
            "scale": "1MON",
            "device_gid": 1001,
            "channel_num": "1,2,3",
            "info": home_vue,
            "usage": 750.0,
            "reset": datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC),
        },
        "1001-Mains_A-1MON": {
            "scale": "1MON",
            "device_gid": 1001,
            "channel_num": "Mains_A",
            "info": home_vue,
            "usage": 375.0,
            "reset": datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC),
        },
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator_1min": coord_1min,
        "coordinator_1mon": coord_mon,
        "coordinator_day_sensor": coord_day,
        "coordinator_device_status": None,
        "retry_update_methods": {},
        "device_information": split_phase_devices,
    }

    added_entities = []

    def mock_add_entities(entities) -> None:
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, mock_add_entities)

    # Aggregate 1,2,3 power sensor
    root_power = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "1,2,3" and e._scale == "1MIN"
        ),
        None,
    )
    assert root_power is not None
    assert root_power.name == "Power Minute Average"
    assert root_power.unique_id == "sensor.emporia_vue.instant.1001-1,2,3"
    assert root_power.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}

    # Aggregate 1,2,3 day energy sensor
    root_day = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "1,2,3" and e._scale == "1D"
        ),
        None,
    )
    assert root_day is not None
    assert root_day.name == "Energy Today"
    assert root_day.unique_id == "sensor.emporia_vue.1D.1001-1,2,3"
    assert root_day.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}

    # Aggregate 1,2,3 month energy sensor
    root_mon = next(
        (
            e
            for e in added_entities
            if e._channel.channel_num == "1,2,3" and e._scale == "1MON"
        ),
        None,
    )
    assert root_mon is not None
    assert root_mon.name == "Energy This Month"
    assert root_mon.unique_id == "sensor.emporia_vue.1MON.1001-1,2,3"
    assert root_mon.device_info["identifiers"] == {(DOMAIN, "1001-1,2,3")}

    # Assert that Mains_A does NOT have Day or Month energy sensors
    day_mains_a = [
        e
        for e in added_entities
        if e._channel.channel_num == "Mains_A" and e._scale == "1D"
    ]
    assert len(day_mains_a) == 0

    mon_mains_a = [
        e
        for e in added_entities
        if e._channel.channel_num == "Mains_A" and e._scale == "1MON"
    ]
    assert len(mon_mains_a) == 0


async def test_coordinator_v1_polling_and_voltage_population(
    hass: HomeAssistant, split_phase_devices: dict[int, VueDevice]
) -> None:
    """Validate coordinator fetches power and Unit.VOLTS voltage telemetry."""
    vue_mock = MagicMock()

    # Setup mock VueUsageDevice objects
    power_device = VueUsageDevice(timestamp=datetime.now(UTC))
    power_device.device_gid = 1001
    power_device.channels = {
        "1,2,3": VueDeviceChannelUsage(
            gid=1001, channelNum="1,2,3", usage=0.0020296568
        ),
        "Mains_A": VueDeviceChannelUsage(
            gid=1001, channelNum="Mains_A", usage=0.001015
        ),
        "Mains_B": VueDeviceChannelUsage(
            gid=1001, channelNum="Mains_B", usage=0.0010146568
        ),
        "Merged_100": VueDeviceChannelUsage(
            gid=1001, channelNum="Merged_100", usage=0.0003
        ),
    }

    voltage_device = VueUsageDevice(timestamp=datetime.now(UTC))
    voltage_device.device_gid = 1001
    voltage_device.channels = {
        "1,2,3": VueDeviceChannelUsage(gid=1001, channelNum="1,2,3", usage=120.75),
        "Mains_A": VueDeviceChannelUsage(gid=1001, channelNum="Mains_A", usage=120.4),
        "Mains_B": VueDeviceChannelUsage(gid=1001, channelNum="Mains_B", usage=121.1),
    }

    def mock_get_device_list_usage_v1(
        device_gids, instant=None, scale="MINUTE", energy_unit=None
    ):
        if energy_unit == Unit.VOLTS:
            return {1001: voltage_device}
        return {1001: power_device}

    vue_mock.get_device_list_usage_v1.side_effect = mock_get_device_list_usage_v1

    # Temporarily set module DEVICE_GIDS and DEVICE_INFORMATION
    from custom_components.emporia_vue import (
        DEVICE_GIDS as MOD_GIDS,
    )
    from custom_components.emporia_vue import (
        DEVICE_INFORMATION as MOD_INFO,
    )

    MOD_GIDS.clear()
    MOD_GIDS.append("1001")
    MOD_INFO.clear()
    MOD_INFO[1001] = split_phase_devices[1001]

    data = await update_sensors(vue_mock, [Scale.MINUTE.value])

    assert "1001-Mains_A-1MIN" in data
    assert data["1001-Mains_A-1MIN"]["voltage"] == pytest.approx(120.4)
    assert data["1001-Mains_A-1MIN"]["usage"] == pytest.approx(0.001015)

    assert "1001-Mains_B-1MIN" in data
    assert data["1001-Mains_B-1MIN"]["voltage"] == pytest.approx(121.1)
    assert data["1001-Mains_B-1MIN"]["usage"] == pytest.approx(0.0010146568)

    assert "1001-Merged_100-1MIN" in data
    assert data["1001-Merged_100-1MIN"]["usage"] == pytest.approx(0.0003)
    assert data["1001-Merged_100-1MIN"]["voltage"] is None


def test_determine_circuit_model_merged_variants() -> None:
    """Test determine_circuit_model handles merged virtual circuits and prefixes."""
    ch_merged_virtual = VueDeviceChannel(gid=1001, channelNum="Merged_100")
    assert determine_circuit_model(ch_merged_virtual) == "Merged virtual circuit"

    ch_merged_virtual_2 = VueDeviceChannel(gid=1001, channelNum="Merged_200")
    assert determine_circuit_model(ch_merged_virtual_2) == "Merged virtual circuit"

    ch_regular_merged = VueDeviceChannel(gid=1001, channelNum="5")
    ch_regular_merged.channel_classification = "MERGED"
    assert determine_circuit_model(ch_regular_merged) == "Merged CT circuit (Branch 5)"

    # With constituent branch discovery from device channel tree
    device = VueDevice(gid=1001)
    ch1 = VueDeviceChannel(gid=1001, channelNum="1")
    ch1.parent_channel_id = "Merged_100"
    ch2 = VueDeviceChannel(gid=1001, channelNum="2")
    ch2.parent_channel_id = "Merged_100"
    device.channels = [ch1, ch2, ch_merged_virtual]
    assert (
        determine_circuit_model(ch_merged_virtual, device)
        == "Merged virtual circuit (Branches 1, 2)"
    )
