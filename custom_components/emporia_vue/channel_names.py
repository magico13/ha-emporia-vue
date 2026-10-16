"""Helpers for refreshing Emporia channel labels without changing sensor IDs."""


def merge_device_information(devices):
    """Index fresh device metadata and merge duplicate-GID channel lists."""
    information = {}
    for device in devices:
        existing = information.get(device.device_gid)
        if existing is None:
            information[device.device_gid] = device
        else:
            existing.channels.extend(device.channels)
    return information


def channel_display_name(device, channel):
    """Return the app-defined channel label, with the integration's old fallback."""
    if channel.name:
        return channel.name
    if str(channel.channel_num).isdigit():
        return f"{device.device_name} Circuit {channel.channel_num}"
    return device.device_name


def refresh_channel_names(information, devices):
    """Update labels in place, preserving IDs, telemetry and synthetic channels."""
    refreshed = merge_device_information(devices)
    updates = []
    for gid, device in information.items():
        latest = refreshed.get(gid)
        if latest is None:
            continue
        channels = {str(channel.channel_num): channel for channel in latest.channels}
        updates.append((device, "device_name", latest.device_name))
        for channel in device.channels:
            fresh = channels.get(str(channel.channel_num))
            if fresh is not None:
                updates.append((channel, "name", fresh.name))
    # Build the complete update first so malformed metadata cannot partially apply.
    for obj, attribute, value in updates:
        setattr(obj, attribute, value)
