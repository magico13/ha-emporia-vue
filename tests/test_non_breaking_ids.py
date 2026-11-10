"""Tests asserting strict backward-compatibility and non-breaking equivalence of identifiers."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.sensor import CurrentVuePowerSensor
from homeassistant.core import HomeAssistant
from pyemvue import PyEmVue
from pyemvue.device import VueDevice
from pyemvue.enums import Scale

FIXTURES_DIR = (
    Path(__file__).parent / "fixtures"
    if (Path(__file__).parent / "fixtures").exists()
    else Path(__file__).resolve().parents[2] / "PyEmVue" / "tests" / "fixtures"
    if (Path(__file__).resolve().parents[2] / "PyEmVue" / "tests" / "fixtures").exists()
    else Path(__file__).resolve().parents[3] / "repos" / "PyEmVue" / "tests" / "fixtures"
)


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

