"""Compatibility shims for Emporia's migrated /v1 API.

PyEmVue 0.18.x still targets the legacy endpoints and camelCase payloads.
The Emporia web/app now uses /v1 paths, Authorization Bearer tokens, and
snake_case JSON. This module patches a PyEmVue instance in-place so the
Home Assistant integration can keep using the existing PyEmVue object model.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Optional, Union

from dateutil.parser import parse
from pyemvue import PyEmVue
from pyemvue.auth import Auth
from pyemvue.customer import Customer
from pyemvue.device import (
    ChargerDevice,
    OutletDevice,
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Unit
import requests

_LOGGER = logging.getLogger(__name__)

# v1 /customers/devices/usages uses word-style scales, not the AppAPI tokens.
_V1_USAGE_SCALE = {
    "1S": "SECOND",
    "1MIN": "MINUTE",
    "15MIN": "MINUTES_15",
    "1H": "HOUR",
    "1D": "DAY",
    "1W": "WEEK",
    "1MON": "MONTH",
    "1Y": "YEAR",
}

_V1_ENERGY_UNIT = {
    Unit.KWH.value: "KILOWATT_HOURS",
    "KilowattHours": "KILOWATT_HOURS",
    "KILOWATT_HOURS": "KILOWATT_HOURS",
}


def apply_v1_compatibility(vue: PyEmVue) -> PyEmVue:
    """Patch a PyEmVue instance to speak Emporia's /v1 API."""
    if getattr(vue, "_emporia_v1_patched", False):
        return vue

    vue._v1_device_ids = []  # type: ignore[attr-defined]
    vue._emporia_v1_patched = True  # type: ignore[attr-defined]

    _patch_auth_class()
    vue.get_customer_details = _get_customer_details.__get__(vue, PyEmVue)  # type: ignore[method-assign]
    vue.get_devices = _get_devices.__get__(vue, PyEmVue)  # type: ignore[method-assign]
    vue.get_device_list_usage = _get_device_list_usage.__get__(vue, PyEmVue)  # type: ignore[method-assign]
    vue.get_devices_status = _get_devices_status.__get__(vue, PyEmVue)  # type: ignore[method-assign]
    vue.get_chart_usage = _get_chart_usage.__get__(vue, PyEmVue)  # type: ignore[method-assign]

    _LOGGER.info("Applied Emporia API v1 compatibility patches to PyEmVue")
    return vue


def _patch_auth_class() -> None:
    """Send Authorization Bearer in addition to the legacy authtoken header."""
    if getattr(Auth, "_emporia_v1_auth_patched", False):
        return

    original = Auth._do_request

    def _do_request(self: Auth, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.get("headers")
        headers = {} if headers is None else dict(headers)
        id_token = self.tokens["id_token"]
        headers["authtoken"] = id_token
        headers["Authorization"] = f"Bearer {id_token}"
        kwargs["headers"] = headers
        return requests.request(
            method,
            f"{self.host}/{path}",
            **kwargs,
            timeout=(self.connect_timeout, self.read_timeout),
        )

    Auth._do_request = _do_request  # type: ignore[method-assign]
    Auth._emporia_v1_auth_patched = True  # type: ignore[attr-defined]
    Auth._emporia_v1_original_do_request = original  # type: ignore[attr-defined]


def _format_time(value: datetime.datetime) -> str:
    if value.tzinfo and value.tzinfo.utcoffset(value) is not None:
        value = value.astimezone(datetime.timezone.utc)
    else:
        value = value.replace(tzinfo=datetime.timezone.utc)
    value = value.replace(tzinfo=None)
    return value.isoformat() + "Z"


def _normalize_channel_num(
    channel_id: Optional[str], classification: Optional[str]
) -> str:
    """Map v1 channel ids onto the legacy numbers HA entity IDs expect."""
    if classification == "MAIN" or channel_id in (None, "", "Mains", "Main"):
        return "1,2,3"
    return str(channel_id)


def _transform_channel(device_gid: int, channel: dict[str, Any]) -> dict[str, Any]:
    channel_num = _normalize_channel_num(
        channel.get("channel_id"),
        channel.get("channel_classification"),
    )
    name = channel.get("name")
    if not name:
        name = "Main" if channel_num == "1,2,3" else channel_num
    return {
        "deviceGid": device_gid,
        "name": name,
        "channelNum": channel_num,
        "channelMultiplier": channel.get("multiplier", 1.0),
        "channelTypeGid": channel.get("channel_type_gid"),
        "type": channel.get("channel_classification") or "",
        "parentChannelNum": channel.get("parent_channel_id"),
    }


def _transform_device(device: dict[str, Any]) -> dict[str, Any]:
    device_gid = device.get("device_gid") or 0
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in device.get("channels") or []:
        transformed = _transform_channel(device_gid, raw)
        channel_num = transformed["channelNum"]
        if channel_num in seen:
            continue
        seen.add(channel_num)
        channels.append(transformed)

    device_type = (device.get("device_type") or "").upper()
    if device_type == "OUTLET" and not channels:
        channels.append(
            {
                "deviceGid": device_gid,
                "name": device.get("display_name") or "Outlet",
                "channelNum": "1,2,3",
                "channelMultiplier": 1.0,
                "channelTypeGid": None,
            }
        )

    lat = device.get("latitude")
    lon = device.get("longitude")
    lat_lon = None
    if lat is not None or lon is not None:
        lat_lon = {"latitude": lat or 0, "longitude": lon or 0}

    result: dict[str, Any] = {
        "deviceGid": device_gid,
        "manufacturerDeviceId": device.get("device_id"),
        "model": device.get("model"),
        "firmware": device.get("firmware"),
        "parentDeviceGid": device.get("parent_device_id") or 0,
        "parentChannelNum": device.get("parent_channel_id") or "",
        "channels": channels,
        "locationProperties": {
            "deviceName": device.get("display_name")
            or device.get("device_id")
            or str(device_gid),
            "displayName": device.get("display_name") or "",
            "timeZone": device.get("time_zone") or "",
            "usageCentPerKwHour": device.get("usage_cent_per_kw_hour") or 0,
            "billingCycleStartDay": device.get("billing_cycle_start_day") or 0,
            "utilityRateGid": device.get("utility_rate_gid"),
            "latitudeLongitude": lat_lon,
        },
    }

    if device_type == "OUTLET":
        result["outlet"] = {
            "deviceGid": device_gid,
            "outletOn": False,
            "loadGid": device.get("load_gid") or 0,
        }
    elif device_type in {"EVSE", "EV_CHARGER", "CHARGER", "EVCHARGER"}:
        result["evCharger"] = {
            "deviceGid": device_gid,
            "chargerOn": False,
            "loadGid": device.get("load_gid") or 0,
        }

    return result


def _transform_customer(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "customerGid": payload.get("customer_gid"),
        "email": payload.get("email"),
        "firstName": payload.get("first_name"),
        "lastName": payload.get("last_name"),
        "createdAt": payload.get("created_at"),
    }


def _transform_nested_devices(nested_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nested = []
    for nested_device in nested_devices:
        nested.append(
            {
                "deviceGid": nested_device.get("device_gid"),
                "channelUsages": [
                    {
                        "deviceGid": nested_channel.get("device_gid"),
                        "channelNum": _normalize_channel_num(
                            nested_channel.get("channel_id"),
                            nested_channel.get("channel_classification"),
                        ),
                        "usage": nested_channel.get("usage"),
                        "percentage": nested_channel.get("percentage", 0.0),
                        "name": nested_channel.get("name"),
                        "nestedDevices": [],
                    }
                    for nested_channel in nested_device.get("channel_usages") or []
                ],
            }
        )
    return nested


def _transform_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    devices = []
    for device in payload.get("device_usages") or []:
        channel_usages = []
        for channel in device.get("channel_usages") or []:
            channel_usages.append(
                {
                    "deviceGid": channel.get("device_gid"),
                    "channelNum": _normalize_channel_num(
                        channel.get("channel_id"),
                        channel.get("channel_classification"),
                    ),
                    "usage": channel.get("usage"),
                    "percentage": channel.get("percentage", 0.0),
                    "name": channel.get("name"),
                    "nestedDevices": _transform_nested_devices(
                        channel.get("nested_devices") or []
                    ),
                }
            )
        devices.append(
            {
                "deviceGid": device.get("device_gid"),
                "channelUsages": channel_usages,
            }
        )

    return {
        "deviceListUsages": {
            "instant": payload.get("instant"),
            "devices": devices,
        }
    }


def _transform_outlet(outlet: dict[str, Any]) -> dict[str, Any]:
    return {
        "deviceGid": outlet.get("device_gid"),
        "outletOn": outlet.get("outlet_on", False),
        "loadGid": outlet.get("load_gid") or 0,
    }


def _transform_evse(evse: dict[str, Any]) -> dict[str, Any]:
    return {
        "deviceGid": evse.get("device_gid"),
        "loadGid": evse.get("load_gid") or 0,
        "chargerOn": evse.get("charger_on", False),
        "message": evse.get("message") or "",
        "status": evse.get("status") or "",
        "icon": evse.get("icon") or "",
        "iconLabel": evse.get("icon_label") or "",
        "iconDetailText": evse.get("icon_detail_text") or "",
        "faultText": evse.get("fault_text") or "",
        "chargingRate": evse.get("charging_rate") or 0,
        "maxChargingRate": evse.get("max_charging_rate") or 0,
        "offPeakSchedulesEnabled": evse.get("off_peak_schedules_enabled", False),
        "debugCode": evse.get("debug_code") or "",
        "proControlCode": evse.get("pro_control_code") or "",
        "breakerPIN": evse.get("breaker_pin") or "",
    }


def _transform_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    connected = []
    for device in payload.get("devices_connected") or []:
        connected.append(
            {
                "deviceGid": device.get("device_gid"),
                "connected": device.get("connected", False),
                "offlineSince": device.get("offline_since"),
            }
        )
    return {
        "devicesConnected": connected,
        "outlets": [_transform_outlet(o) for o in (payload.get("outlets") or [])],
        "evChargers": [_transform_evse(e) for e in (payload.get("evses") or [])],
    }


def _get_customer_details(self: PyEmVue) -> Optional[Customer]:
    response = self.auth.request("get", "v1/customers")
    response.raise_for_status()
    if not response.text:
        return None
    return Customer().from_json_dictionary(_transform_customer(response.json()))


def _get_devices(self: PyEmVue) -> list[VueDevice]:
    response = self.auth.request("get", "v1/customers/devices")
    response.raise_for_status()
    devices: list[VueDevice] = []
    device_ids: list[str] = []
    if response.text:
        payload = response.json()
        for raw in payload.get("devices") or []:
            device_id = raw.get("device_id")
            if device_id:
                device_ids.append(str(device_id))
            devices.append(VueDevice().from_json_dictionary(_transform_device(raw)))
            for sub in raw.get("devices") or []:
                sub_id = sub.get("device_id")
                if sub_id:
                    device_ids.append(str(sub_id))
                devices.append(
                    VueDevice().from_json_dictionary(_transform_device(sub))
                )
    self._v1_device_ids = device_ids  # type: ignore[attr-defined]
    return devices


def _get_device_list_usage(
    self: PyEmVue,
    deviceGids: Union[str, list[str]],
    instant: Optional[datetime.datetime],
    scale: str = "1S",
    unit: str = Unit.KWH.value,
    max_retry_attempts: int = 5,
    initial_retry_delay: float = 2.0,
    max_retry_delay: float = 30.0,
) -> dict[int, VueUsageDevice]:
    if not instant:
        instant = datetime.datetime.now(datetime.timezone.utc)
    gids = deviceGids
    if isinstance(deviceGids, list):
        gids = ",".join(map(str, deviceGids))

    v1_scale = _V1_USAGE_SCALE.get(scale, scale)
    v1_unit = _V1_ENERGY_UNIT.get(unit, unit)
    scale_candidates = [v1_scale]
    if scale not in scale_candidates:
        scale_candidates.append(scale)

    attempts = 0
    update_failed = True
    max_retry_attempts = max(max_retry_attempts, 1)
    initial_retry_delay = max(initial_retry_delay, 0.5)
    max_retry_delay = max(max_retry_delay, 0)
    devices: dict[int, VueUsageDevice] = {}
    response = None
    active_scale = scale_candidates[0]

    while attempts < max_retry_attempts and update_failed:
        update_failed = False
        if attempts > 0:
            delay = min(initial_retry_delay * (2 ** (attempts - 1)), max_retry_delay)
            time.sleep(delay)
        attempts += 1
        url = (
            "v1/customers/devices/usages"
            f"?device_gids={gids}"
            f"&instant={_format_time(instant)}"
            f"&scale={active_scale}"
            f"&energy_unit={v1_unit}"
        )
        response = self.auth.request("get", url)
        if response.status_code == 400 and len(scale_candidates) > 1:
            # Some v1 builds want SECOND/MINUTE; others still accept 1S/1MIN.
            next_scale = scale_candidates[(scale_candidates.index(active_scale) + 1) % len(scale_candidates)]
            _LOGGER.warning(
                "Usage scale %s rejected by v1 API; retrying with %s",
                active_scale,
                next_scale,
            )
            active_scale = next_scale
            update_failed = True
            continue
        if response.status_code == 200 and response.text:
            payload = _transform_usage_payload(response.json())
            usages = payload.get("deviceListUsages") or {}
            if "devices" in usages:
                timestamp = (
                    parse(usages["instant"]) if usages.get("instant") else instant
                )
                for device in usages["devices"]:
                    populated = VueUsageDevice(
                        timestamp=timestamp
                    ).from_json_dictionary(device)
                    data_missing = any(
                        channel_usage.usage is None
                        for channel_usage in populated.channels.values()
                    )
                    update_failed = update_failed or data_missing
                    if not data_missing or attempts >= max_retry_attempts:
                        devices[populated.device_gid] = populated
            else:
                update_failed = True
        else:
            update_failed = True

    if response is not None:
        response.raise_for_status()
    return devices


def _get_devices_status(
    self: PyEmVue,
    device_list: Optional[list[VueDevice]] = None,
) -> tuple[list[OutletDevice], list[ChargerDevice]]:
    path = "v1/customers/devices/status"
    device_ids = list(getattr(self, "_v1_device_ids", []) or [])
    if not device_ids and device_list:
        device_ids = [
            str(device.manufacturer_id)
            for device in device_list
            if getattr(device, "manufacturer_id", None)
        ]
    if device_ids:
        query = "&".join(f"device_ids={device_id}" for device_id in device_ids)
        path = f"{path}?{query}"

    response = self.auth.request("get", path)
    response.raise_for_status()
    chargers: list[ChargerDevice] = []
    outlets: list[OutletDevice] = []
    if response.text:
        payload = _transform_status_payload(response.json())
        for raw_charger in payload.get("evChargers") or []:
            chargers.append(ChargerDevice().from_json_dictionary(raw_charger))
        for raw_outlet in payload.get("outlets") or []:
            outlets.append(OutletDevice().from_json_dictionary(raw_outlet))
        if device_list and payload.get("devicesConnected"):
            for raw_device_data in payload["devicesConnected"]:
                if not raw_device_data or not raw_device_data.get("deviceGid"):
                    continue
                for device in device_list:
                    if device.device_gid == raw_device_data["deviceGid"]:
                        device.connected = raw_device_data.get("connected", False)
                        offline = raw_device_data.get("offlineSince")
                        if offline:
                            try:
                                device.offline_since = parse(offline)
                            except (TypeError, ValueError, OverflowError):
                                device.offline_since = datetime.datetime.min
                        break
    return (outlets, chargers)


def _get_chart_usage(
    self: PyEmVue,
    channel: Union[VueDeviceChannel, VueDeviceChannelUsage],
    start: Optional[datetime.datetime] = None,
    end: Optional[datetime.datetime] = None,
    scale: str = "1S",
    unit: str = Unit.KWH.value,
) -> tuple[list[float], Optional[datetime.datetime]]:
    if channel.channel_num in ["MainsFromGrid", "MainsToGrid"]:
        return [], start
    if not start:
        start = datetime.datetime.now(datetime.timezone.utc)
    if not end:
        end = datetime.datetime.now(datetime.timezone.utc)

    channel_arg = channel.channel_num
    if channel_arg == "1,2,3":
        channel_arg = "Mains"

    url = (
        "v1/migrated/app-api/chart-usage"
        f"?deviceGid={channel.device_gid}"
        f"&channel={channel_arg}"
        f"&start={_format_time(start)}"
        f"&end={_format_time(end)}"
        f"&scale={scale}"
        f"&energyUnit={unit}"
    )
    response = self.auth.request("get", url)
    response.raise_for_status()
    usage: list[float] = []
    instant: datetime.datetime | None = start
    if response.text:
        payload = response.json()
        if "firstUsageInstant" in payload:
            instant = parse(payload["firstUsageInstant"])
        if "usageList" in payload:
            usage = payload["usageList"]
    return usage, instant
