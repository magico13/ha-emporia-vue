"""Tests for selecting a subset of devices from an Emporia account."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "emporia_vue" / "device_filter.py"
)
SPEC = spec_from_file_location("emporia_vue_device_filter", MODULE_PATH)
assert SPEC and SPEC.loader
DEVICE_FILTER = module_from_spec(SPEC)
SPEC.loader.exec_module(DEVICE_FILTER)


def make_device(
    gid: int,
    *,
    parent_gid: int = 0,
    name: str = "",
    model: str = "Vue 3",
) -> SimpleNamespace:
    """Create the small device shape used by the filtering helpers."""
    return SimpleNamespace(
        device_gid=gid,
        parent_device_gid=parent_gid,
        device_name=name,
        display_name="",
        model=model,
    )


def test_selectable_devices_only_lists_unique_roots() -> None:
    """Nested devices are included with their parent, not selected separately."""
    first = make_device(100, name="House A")
    devices = [first, make_device(200, parent_gid=100), first, make_device(300)]

    assert DEVICE_FILTER.selectable_devices(devices) == [first, devices[-1]]


def test_device_label_includes_name_and_gid() -> None:
    """Two similarly named devices remain distinguishable in the form."""
    assert DEVICE_FILTER.device_label(make_device(100, name="Main Panel")) == (
        "Main Panel (100)"
    )


def test_filter_includes_selected_roots_and_descendants() -> None:
    """Selecting a monitor also includes smart devices nested below it."""
    house_a = make_device(100, name="House A")
    house_a_plug = make_device(110, parent_gid=100)
    nested_device = make_device(111, parent_gid=110)
    house_b = make_device(200, name="House B")

    assert DEVICE_FILTER.filter_selected_devices(
        [house_a, house_a_plug, nested_device, house_b], ["100"]
    ) == [house_a, house_a_plug, nested_device]


def test_filter_supports_multiple_selected_roots() -> None:
    """One Home Assistant instance can deliberately include multiple monitors."""
    house_a = make_device(100, name="House A")
    house_b = make_device(200, name="House B")
    house_c = make_device(300, name="House C")

    assert DEVICE_FILTER.filter_selected_devices(
        [house_a, house_b, house_c], ["100", "300"]
    ) == [house_a, house_c]


def test_missing_selection_preserves_legacy_include_all_behavior() -> None:
    """Existing entries created before filtering continue to expose all devices."""
    devices = [make_device(100), make_device(200)]

    assert DEVICE_FILTER.filter_selected_devices(devices, None) == devices
    assert DEVICE_FILTER.filter_selected_devices(devices, []) == []
