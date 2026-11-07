"""Tests for real-time SSE push coordinator in ha-emporia-vue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyemvue.device import ChargerDevice, EvseControlStatus, OutletDevice, VueDevice
from pyemvue.stream import DeviceStatusDelta, EmporiaStreamClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue import (
    DOMAIN,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.emporia_vue.const import (
    AUTH_METHOD,
    AUTH_METHOD_TOKENS,
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock Emporia Vue config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Emporia Test",
        data={
            AUTH_METHOD: AUTH_METHOD_TOKENS,
            "key": "test_key",
            CONF_ID_TOKEN: "mock_id_token",
            CONF_ACCESS_TOKEN: "mock_access_token",
            CONF_REFRESH_TOKEN: "mock_refresh_token",
            ENABLE_1M: False,
            ENABLE_1D: False,
            ENABLE_1MON: False,
        },
        entry_id="test_entry_sse_123",
    )


@pytest.fixture
def mock_vue_with_controllable_devices() -> MagicMock:
    """Return a mock PyEmVue client with an EV charger and an outlet."""
    vue = MagicMock()
    vue.login.return_value = True
    vue.down_for_maintenance.return_value = None
    vue.auth.tokens = {
        CONF_ID_TOKEN: "mock_id_token",
        CONF_ACCESS_TOKEN: "mock_access_token",
        CONF_REFRESH_TOKEN: "mock_refresh_token",
    }

    charger_dev = VueDevice(gid=1003, modelNum="EVSE-001", firmwareVersion="2.1.0")
    charger_dev.device_name = "Garage Charger"
    charger_dev.ev_charger = ChargerDevice(gid=1003, on=True)

    outlet_dev = VueDevice(gid=2001, modelNum="PLUG-001", firmwareVersion="1.0.0")
    outlet_dev.device_name = "Living Room Plug"
    outlet_dev.outlet = OutletDevice(gid=2001, on=True)

    vue.get_devices_v1.return_value = [charger_dev, outlet_dev]
    vue.get_devices.return_value = [charger_dev, outlet_dev]

    # Status call
    active_charger = ChargerDevice(gid=1003, on=True)
    active_charger.status = "Standby"
    active_outlet = OutletDevice(gid=2001, on=True)
    vue.get_devices_status.return_value = ([active_outlet], [active_charger])

    ctrl_status = EvseControlStatus(
        device_id="evse-1003",
        session_active=False,
        charging_rate_kw=0.0,
        charging_rate_amps=0.0,
    )
    vue.get_evse_control.return_value = ctrl_status

    vue.get_excess_generation_monitors.return_value = None
    return vue


@pytest.mark.asyncio
async def test_sse_stream_initialization_and_lifecycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test background SSE stream client creation and clean unload cancellation."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.close = AsyncMock()

        async def empty_stream():
            if False:
                yield None

        mock_client.listen.return_value = empty_stream()
        mock_stream_client_cls.return_value = mock_client

        # Setup entry
        success = await async_setup_entry(hass, mock_config_entry)
        assert success is True

        entry_data = hass.data[DOMAIN][mock_config_entry.entry_id]
        stream_client = entry_data.get("stream_client")
        stream_task = entry_data.get("stream_task")

        assert stream_client is mock_client
        assert stream_task is not None
        assert mock_stream_client_cls.call_count == 1

        # Unload entry
        unload_ok = await async_unload_entry(hass, mock_config_entry)
        assert unload_ok is True
        assert mock_client.close.call_count >= 1
        assert mock_config_entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_sse_stream_delta_updates_coordinator_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test that incoming stream deltas update coordinator_device_status data instantaneously."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    event_queue: asyncio.Queue[DeviceStatusDelta] = asyncio.Queue()

    async def sse_event_generator():
        while True:
            delta = await event_queue.get()
            yield delta
            event_queue.task_done()

    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.close = AsyncMock()
        mock_client.listen.side_effect = sse_event_generator
        mock_stream_client_cls.return_value = mock_client

        success = await async_setup_entry(hass, mock_config_entry)
        assert success is True

        entry_data = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator: DataUpdateCoordinator = entry_data["coordinator_device_status"]
        assert coordinator is not None

        # Verify initial state
        assert coordinator.data["1003"].status == "Standby"

        listener_notified = asyncio.Event()

        def on_coordinator_update():
            listener_notified.set()

        unsub = coordinator.async_add_listener(on_coordinator_update)

        # Dispatch an EVSE status delta
        evse_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS.DELTA",
            {
                "device_gid": 1003,
                "status": "CHARGING",
                "charger_on": True,
                "vehicle_connected": True,
                "vehicle_charging": True,
                "charging_rate_kw": 9.6,
                "charging_rate_amps": 40.0,
                "offered_amps": 40.0,
                "energy_delivered": 18.5,
                "active_charging_minutes": 85,
                "cost": 2.34,
                "plug_in": "2026-11-07T00:00:00Z",
            },
        )
        await event_queue.put(evse_delta)

        # Wait for listener notification
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)

        # Verify updated data in coordinator
        charger: ChargerDevice = coordinator.data["1003"]
        assert charger.status == "CHARGING"
        assert charger.vehicle_connected is True
        assert charger.vehicle_charging is True
        assert charger.charger_on is True

        ctrl = charger.evse_control
        assert ctrl is not None
        assert ctrl.charging_rate_kw == 9.6
        assert ctrl.charging_rate_amps == 40.0
        assert ctrl.offered_amps == 40.0
        assert ctrl.energy_delivered == 18.5
        assert ctrl.active_charging_minutes == 85
        assert ctrl.cost == 2.34
        assert ctrl.plug_in == "2026-11-07T00:00:00Z"
        assert ctrl.session_active is True
        assert ctrl.charger_status == "CHARGING"
        assert ctrl.button_action == "PAUSE"

        # Dispatch a pause delta from Emporia mobile app (wrapped in data envelope)
        listener_notified.clear()
        pause_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS",
            {
                "data": {
                    "evses": [
                        {
                            "device_id": "evse-1003",
                            "device_gid": 1003,
                            "status": "CONNECTED_OFF",
                            "charger_on": False,
                        }
                    ]
                }
            },
        )
        await event_queue.put(pause_delta)
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)
        assert charger.status == "CONNECTED_OFF"
        assert charger.charger_on is False
        assert ctrl.charger_status == "CONNECTED_OFF"
        assert ctrl.button_action == "RESUME"
        assert ctrl.session_active is False

        # 4. Verify resilient exception isolation: malformed delta does not kill listener loop
        listener_notified.clear()
        bad_delta = MagicMock()
        bad_delta.event_type = "DEVICE_STATUS.DELTA"
        bad_delta.device_gid = 1003
        bad_evse = MagicMock()
        type(bad_evse).device_gid = property(
            MagicMock(side_effect=RuntimeError("Corrupted payload field"))
        )
        bad_delta.evses = [bad_evse]
        await event_queue.put(bad_delta)
        await asyncio.sleep(0.05)

        # Subsequent valid delta must still be received and processed
        resume_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS",
            {
                "data": {
                    "evses": [
                        {
                            "device_id": "evse-1003",
                            "device_gid": 1003,
                            "status": "CHARGING",
                            "charger_on": True,
                        }
                    ]
                }
            },
        )
        await event_queue.put(resume_delta)
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)
        assert charger.status == "CHARGING"
        assert charger.charger_on is True

        # 5. Production cloud event: solar throttling delta without embedded telemetry triggers live evse_control refresh
        listener_notified.clear()
        mock_vue_with_controllable_devices.get_evse_control.return_value = EvseControlStatus(
            device_id="evse-1003",
            session_active=True,
            charging_rate_amps=11.0,
            energy_delivered=7.25,
        )
        solar_throttle_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS",
            {
                "data": {
                    "evses": [
                        {
                            "device_id": "evse-1003",
                            "device_gid": 1003,
                            "status": "CHARGING",
                            "charging_rate": 13,
                            "charger_on": True,
                        }
                    ]
                }
            },
        )
        await event_queue.put(solar_throttle_delta)
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)
        assert charger.charging_rate == 13
        assert charger.evse_control.charging_rate_amps == 11.0
        assert charger.evse_control.energy_delivered == 7.25

        unsub()
        await async_unload_entry(hass, mock_config_entry)


@pytest.mark.asyncio
async def test_sse_stream_delta_outlet_updates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test that incoming stream deltas update outlet switch state immediately."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    event_queue: asyncio.Queue[DeviceStatusDelta] = asyncio.Queue()

    async def sse_event_generator():
        while True:
            delta = await event_queue.get()
            yield delta
            event_queue.task_done()

    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.close = AsyncMock()
        mock_client.listen.side_effect = sse_event_generator
        mock_stream_client_cls.return_value = mock_client

        await async_setup_entry(hass, mock_config_entry)
        coordinator: DataUpdateCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator_device_status"]

        # Initial outlet state
        assert coordinator.data["2001"].outlet_on is True

        listener_notified = asyncio.Event()
        coordinator.async_add_listener(listener_notified.set)

        # Dispatch an outlet state change
        outlet_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS.DELTA",
            {
                "device_gid": 2001,
                "outlet_on": False,
            },
        )
        await event_queue.put(outlet_delta)
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)

        outlet: OutletDevice = coordinator.data["2001"]
        assert outlet.outlet_on is False

        await async_unload_entry(hass, mock_config_entry)


@pytest.mark.asyncio
async def test_sse_stream_delta_device_name_sync(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test that stream deltas with device name changes trigger registry update."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    event_queue: asyncio.Queue[DeviceStatusDelta] = asyncio.Queue()

    async def sse_event_generator():
        while True:
            delta = await event_queue.get()
            yield delta
            event_queue.task_done()

    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "custom_components.emporia_vue.async_setup_device_registry"
        ) as mock_reg_sync,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.close = AsyncMock()
        mock_client.listen.side_effect = sse_event_generator
        mock_stream_client_cls.return_value = mock_client

        await async_setup_entry(hass, mock_config_entry)
        coordinator: DataUpdateCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator_device_status"]

        listener_notified = asyncio.Event()
        coordinator.async_add_listener(listener_notified.set)

        # Dispatch name update
        name_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS.DELTA",
            {
                "device_gid": 1003,
                "device_name": "Upgraded Garage EVSE",
            },
        )
        await event_queue.put(name_delta)
        await asyncio.wait_for(listener_notified.wait(), timeout=1.0)

        device_info_map = hass.data[DOMAIN][mock_config_entry.entry_id][
            "device_information"
        ]
        assert device_info_map[1003].device_name == "Upgraded Garage EVSE"
        # async_setup_device_registry called on setup and on delta
        assert mock_reg_sync.call_count >= 2

        await async_unload_entry(hass, mock_config_entry)


@pytest.mark.asyncio
async def test_sse_polling_fallback_resilience(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test that 60-second polling coordinator continues to operate when stream errors."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    # Stream client raises exception
    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.close = AsyncMock()

        async def failing_stream():
            raise ConnectionError("Stream dropped")
            if False:
                yield None

        mock_client.listen.side_effect = failing_stream
        mock_stream_client_cls.return_value = mock_client

        await async_setup_entry(hass, mock_config_entry)
        coordinator: DataUpdateCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator_device_status"]

        # Update returned by polling mock
        polled_charger = ChargerDevice(gid=1003, on=True)
        polled_charger.status = "Charging-Polled"
        polled_outlet = OutletDevice(gid=2001, on=False)
        mock_vue_with_controllable_devices.get_devices_status.return_value = (
            [polled_outlet],
            [polled_charger],
        )

        # Trigger manual refresh via the polling coordinator
        await coordinator.async_refresh()

        assert coordinator.data["1003"].status == "Charging-Polled"
        assert coordinator.data["2001"].outlet_on is False

        await async_unload_entry(hass, mock_config_entry)


@pytest.mark.asyncio
async def test_sse_adaptive_polling_interval_coordination(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vue_with_controllable_devices: MagicMock,
) -> None:
    """Test adaptive polling interval relaxation to 10m on connect, 1m on drop, and refresh on reconnect."""
    from datetime import timedelta

    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    captured_on_connect = None
    captured_on_disconnect = None

    def fake_stream_init(**kwargs):
        nonlocal captured_on_connect, captured_on_disconnect
        captured_on_connect = kwargs.get("on_connect")
        captured_on_disconnect = kwargs.get("on_disconnect")
        mock_instance = MagicMock(spec=EmporiaStreamClient)
        mock_instance.close = AsyncMock()

        async def endless_stream():
            while True:
                await asyncio.sleep(10)
                if False:
                    yield None

        mock_instance.listen.side_effect = endless_stream
        return mock_instance

    with (
        patch(
            "custom_components.emporia_vue.PyEmVue",
            return_value=mock_vue_with_controllable_devices,
        ),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient",
            side_effect=fake_stream_init,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        await async_setup_entry(hass, mock_config_entry)
        coordinator: DataUpdateCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator_device_status"]

        assert captured_on_connect is not None
        assert captured_on_disconnect is not None

        # 1. When EV chargers are present, initial connect retains 1-minute interval for EVSE session telemetry
        captured_on_connect()
        assert coordinator.update_interval == timedelta(minutes=1)

        # 2. Disconnect retains 1-minute fallback
        captured_on_disconnect()
        assert coordinator.update_interval == timedelta(minutes=1)

        # 3. Reconnect triggers immediate refresh and retains 1-minute interval
        with patch.object(
            coordinator, "async_request_refresh", new_callable=AsyncMock
        ) as mock_refresh:
            captured_on_connect()
            assert coordinator.update_interval == timedelta(minutes=1)
            # Allow event loop to process async_create_task
            await asyncio.sleep(0.01)
            assert mock_refresh.call_count == 1

        # 4. For outlet-only setups without EV chargers, connect relaxes interval to 10 minutes
        from custom_components.emporia_vue import DEVICE_INFORMATION
        DEVICE_INFORMATION[1003].ev_charger = None
        captured_on_connect()
        assert coordinator.update_interval == timedelta(minutes=10)

        await async_unload_entry(hass, mock_config_entry)

