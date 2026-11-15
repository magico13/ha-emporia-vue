"""Tests asserting strict backward-compatibility and non-breaking equivalence of identifiers."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice, OutletDevice, VueDevice, VueDeviceChannel
from pyemvue.enums import Scale

from custom_components.emporia_vue import (
    _async_discover_devices,
    _merge_discovered_devices,
)
from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.sensor import CurrentVuePowerSensor

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str):
    with open(FIXTURES_DIR / filename, encoding="utf-8") as f:
        return json.load(f)


async def test_identifier_and_unique_id_equivalence(hass: HomeAssistant) -> None:
    """Assert device identifiers and entity unique IDs match 100% identically between legacy and V1."""
    legacy_json = load_fixture("devices_legacy.json")
    v1_json = load_fixture("devices_v1.json")

    # Parse legacy devices
    legacy_devices: list[VueDevice] = []
    for d in legacy_json.get("devices", []):
        legacy_devices.append(VueDevice().from_json_dictionary(d))

    # Parse V1 devices
    v1_vue = PyEmVue()
    v1_vue.auth_v1 = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(v1_json)
    mock_resp.json.return_value = v1_json
    v1_vue.auth_v1.request.return_value = mock_resp
    v1_devices: list[VueDevice] = v1_vue.get_devices_v1()

    # Simulate inventory setup in __init__.py which ensures channel.device_gid is populated
    for d in v1_devices:
        for c in d.channels:
            if not c.device_gid:
                c.device_gid = d.device_gid

    # Create map by GID
    v1_dev_map = {d.device_gid: d for d in v1_devices}

    # Verify device identifiers for all overlapping physical devices
    for legacy_dev in legacy_devices:
        if legacy_dev.device_gid not in v1_dev_map:
            continue
        v1_dev = v1_dev_map[legacy_dev.device_gid]

        # Physical device identifier must be identically (DOMAIN, f"{gid}-1,2,3")
        expected_physical_ident = {(DOMAIN, f"{legacy_dev.device_gid}-1,2,3")}
        v1_physical_ident = {(DOMAIN, f"{v1_dev.device_gid}-1,2,3")}
        assert expected_physical_ident == v1_physical_ident

        # Verify active channel device identifiers and entity unique_ids
        for legacy_ch in legacy_dev.channels:
            # Find matching channel in V1
            v1_ch = next(
                (c for c in v1_dev.channels if c.channel_num == legacy_ch.channel_num),
                None,
            )
            if v1_ch is None:
                continue

            # Check identifier equivalence for CT sub-device
            if legacy_ch.channel_num != "1,2,3":
                expected_ct_ident = {
                    (DOMAIN, f"{legacy_dev.device_gid}-{legacy_ch.channel_num}")
                }
                actual_ct_ident = {(DOMAIN, f"{v1_dev.device_gid}-{v1_ch.channel_num}")}
                assert expected_ct_ident == actual_ct_ident

            # Check unique_id equivalence across all scales
            for scale in [Scale.MINUTE.value, Scale.DAY.value, Scale.MONTH.value]:
                ident_key = f"{legacy_dev.device_gid}-{legacy_ch.channel_num}-{scale}"

                legacy_coord = MagicMock()
                legacy_coord.data = {
                    ident_key: {
                        "scale": scale,
                        "device_gid": legacy_dev.device_gid,
                        "channel_num": legacy_ch.channel_num,
                        "info": legacy_dev,
                        "usage": 100.0,
                    }
                }
                legacy_sensor = CurrentVuePowerSensor(legacy_coord, ident_key)

                v1_coord = MagicMock()
                v1_coord.data = {
                    ident_key: {
                        "scale": scale,
                        "device_gid": v1_dev.device_gid,
                        "channel_num": v1_ch.channel_num,
                        "info": v1_dev,
                        "usage": 100.0,
                    }
                }
                v1_sensor = CurrentVuePowerSensor(v1_coord, ident_key)

                assert legacy_sensor.unique_id == v1_sensor.unique_id
                assert (
                    legacy_sensor.device_info["identifiers"]
                    == v1_sensor.device_info["identifiers"]
                )


async def test_v1_discovery_falls_back_on_exception_and_empty_list(
    hass: HomeAssistant,
) -> None:
    """Ensure get_devices_v1 runtime errors or empty results fall back to get_devices."""
    legacy_dev = VueDevice(gid=2001)
    loop = asyncio.get_running_loop()

    vue_err = MagicMock()
    vue_err.get_devices_v1.side_effect = RuntimeError("v1 temporary error")
    vue_err.get_devices.return_value = [legacy_dev]
    result_err = await _async_discover_devices(vue_err, loop)
    assert result_err == [legacy_dev]
    vue_err.get_devices.assert_called_once()

    vue_empty = MagicMock()
    vue_empty.get_devices_v1.return_value = []
    vue_empty.get_devices.return_value = [legacy_dev]
    result_empty = await _async_discover_devices(vue_empty, loop)
    assert result_empty == [legacy_dev]
    vue_empty.get_devices.assert_called_once()


def test_duplicate_device_gid_merge_deduplicates_channels_and_preserves_devices() -> (
    None
):
    """Duplicate device_gid records merge missing channels without duplicates and keep outlet/charger."""
    d1 = VueDevice(gid=3001)
    d1.channels = [
        VueDeviceChannel(gid=3001, channelNum="1,2,3"),
        VueDeviceChannel(gid=3001, channelNum="1"),
    ]

    d2 = VueDevice(gid=3001)
    d2.channels = [
        VueDeviceChannel(gid=3001, channelNum="1"),  # duplicate channel_num
        VueDeviceChannel(gid=3001, channelNum="2"),  # new channel_num
    ]
    d2.outlet = OutletDevice(gid=3001, on=True)
    d2.ev_charger = ChargerDevice(gid=3001, on=True)

    gids: list[str] = []
    info: dict[int, VueDevice] = {}
    _merge_discovered_devices([d1, d2], gids, info)

    assert gids == ["3001"]
    merged = info[3001]
    assert [ch.channel_num for ch in merged.channels] == ["1,2,3", "1", "2"]
    assert merged.outlet is d2.outlet
    assert merged.ev_charger is d2.ev_charger
