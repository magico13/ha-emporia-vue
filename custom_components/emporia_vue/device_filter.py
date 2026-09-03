"""Helpers for selecting a subset of devices from an Emporia account."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def device_gid(device: Any) -> str:
    """Return a device GID in the string form used by config entries."""
    return str(device.device_gid)


def selectable_devices(devices: Iterable[Any]) -> list[Any]:
    """Return unique top-level devices that can be selected in the config flow."""
    device_list = list(devices)
    unique_devices: dict[str, Any] = {}
    for device in device_list:
        if not getattr(device, "parent_device_gid", None):
            unique_devices.setdefault(device_gid(device), device)

    # An unexpected API response should still give the user a way to select a
    # device instead of presenting an empty form.
    if not unique_devices:
        for device in device_list:
            unique_devices.setdefault(device_gid(device), device)

    return list(unique_devices.values())


def device_label(device: Any) -> str:
    """Build a recognizable, stable label for a device selector option."""
    gid = device_gid(device)
    name = (
        getattr(device, "device_name", "")
        or getattr(device, "display_name", "")
        or getattr(device, "model", "")
        or "Emporia device"
    )
    return f"{name} ({gid})"


def filter_selected_devices(
    devices: Iterable[Any], selected_gids: Iterable[str] | None
) -> list[Any]:
    """Keep selected devices and every device nested below them.

    A missing selection belongs to a config entry created by an older version,
    so it intentionally retains the historical behavior of including all
    devices. An explicitly empty selection includes none.
    """
    device_list = list(devices)
    if selected_gids is None:
        return device_list

    included = {str(gid) for gid in selected_gids}
    parent_by_gid = {
        device_gid(device): str(parent_gid)
        for device in device_list
        if (parent_gid := getattr(device, "parent_device_gid", None))
    }

    changed = True
    while changed:
        changed = False
        for gid, parent_gid in parent_by_gid.items():
            if gid not in included and parent_gid in included:
                included.add(gid)
                changed = True

    return [device for device in device_list if device_gid(device) in included]
