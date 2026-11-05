from dataclasses import dataclass, field
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

MODULE = Path(__file__).parents[1] / "custom_components" / "emporia_vue" / "channel_names.py"
spec = spec_from_file_location("emporia_channel_names", MODULE)
helpers = module_from_spec(spec)
spec.loader.exec_module(helpers)


@dataclass
class Channel:
    channel_num: str
    name: str | None


@dataclass
class Device:
    device_gid: int
    device_name: str
    channels: list[Channel] = field(default_factory=list)


def test_app_rename_in_refreshed_metadata_is_used():
    old = Device(12, "Vue", [Channel("1", "Kitchen")])
    new = Device(12, "Vue", [Channel("1", "Refrigerator")])
    info = helpers.merge_device_information([new])
    assert helpers.channel_display_name(info[12], info[12].channels[0]) == "Refrigerator"
    assert helpers.channel_display_name(old, old.channels[0]) == "Kitchen"


def test_duplicate_device_records_merge_channels():
    first = Device(12, "Vue", [Channel("1", "Kitchen")])
    second = Device(12, "Vue", [Channel("2", "Laundry")])
    result = helpers.merge_device_information([first, second])
    assert list(result) == [12]
    assert [ch.name for ch in result[12].channels] == ["Kitchen", "Laundry"]


def test_unnamed_circuit_and_aggregate_fallbacks_are_preserved():
    device = Device(12, "Main Vue", [])
    assert helpers.channel_display_name(device, Channel("4", None)) == "Main Vue Circuit 4"
    assert helpers.channel_display_name(device, Channel("Balance", None)) == "Main Vue"
    assert helpers.channel_display_name(device, Channel("5", "Garage")) == "Garage"


def test_refresh_preserves_objects_and_synthetic_channels():
    circuit = Channel("1", "Old")
    synthetic = Channel("Balance", "Balance")
    device = Device(12, "Vue", [circuit, synthetic])
    information = {12: device}
    helpers.refresh_channel_names(
        information, [Device(12, "New Vue", [Channel("1", "New")])]
    )
    assert information[12] is device
    assert device.channels == [circuit, synthetic]
    assert device.channels[0] is circuit
    assert circuit.channel_num == "1"
    assert circuit.name == "New"
    assert synthetic.name == "Balance"
    assert device.device_name == "New Vue"


def test_missing_devices_and_channels_keep_last_names():
    circuit = Channel("1", "Kitchen")
    device = Device(12, "Vue", [circuit])
    information = {12: device}
    helpers.refresh_channel_names(information, [])
    helpers.refresh_channel_names(information, [Device(99, "Other", [])])
    helpers.refresh_channel_names(information, [Device(12, "Vue", [])])
    assert list(information) == [12]
    assert device.channels == [circuit]
    assert circuit.name == "Kitchen"


def test_clearing_app_label_restores_numbered_fallback():
    device = Device(12, "Vue", [Channel("1", "Kitchen")])
    helpers.refresh_channel_names({12: device}, [Device(12, "Vue", [Channel("1", "")])])
    assert helpers.channel_display_name(device, device.channels[0]) == "Vue Circuit 1"
