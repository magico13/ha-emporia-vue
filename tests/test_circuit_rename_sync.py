"""Tests for optional automatic circuit rename synchronization (Fixes #336, #374)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pyemvue.device import VueDevice, VueDeviceChannel
from pyemvue.stream import DeviceStatusDelta
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue import (
    EmporiaStreamClient,
    async_setup_device_registry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.emporia_vue.config_flow import validate_input
from custom_components.emporia_vue.const import (
    AUTH_METHOD,
    AUTH_METHOD_EMAIL_PASSWORD,
    CONFIG_TITLE,
    CUSTOMER_GID,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
    SYNC_CIRCUIT_NAMES,
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


def _build_test_devices() -> dict[int, VueDevice]:
    """Create a multi-circuit Vue monitor fixture for rename testing."""
    home_vue = VueDevice(gid=1001, modelNum="VUE003", firmwareVersion="1.0.0")
    home_vue.device_name = "Home Vue"
    home_vue.manufacturer_id = "MANUF-1001"
    ch_main = VueDeviceChannel(
        gid=1001, name=None, channelNum="1,2,3", channelMultiplier=1.0, channelTypeGid=1
    )
    ch_main.has_data = True
    ch1 = VueDeviceChannel(
        gid=1001,
        name="Dishwasher",
        channelNum="1",
        channelMultiplier=1.0,
        channelTypeGid=1,
    )
    ch1.has_data = True
    ch2 = VueDeviceChannel(
        gid=1001,
        name="Oven",
        channelNum="2",
        channelMultiplier=1.0,
        channelTypeGid=1,
    )
    ch2.has_data = True
    ch3 = VueDeviceChannel(
        gid=1001,
        name="Guest Bedroom",
        channelNum="3",
        channelMultiplier=1.0,
        channelTypeGid=1,
    )
    ch3.has_data = True
    home_vue.channels = [ch_main, ch1, ch2, ch3]
    return {1001: home_vue}


async def test_config_flow_and_reconfigure_sync_circuit_names(
    hass: HomeAssistant,
) -> None:
    """Test sync_circuit_names option in validate_input and reconfigure flow."""
    mock_customer = MagicMock()
    mock_customer.email = "user@example.com"
    mock_customer.customer_gid = 12345

    with (
        patch(
            "custom_components.emporia_vue.config_flow.VueHub.authenticate",
            return_value=True,
        ),
        patch("custom_components.emporia_vue.config_flow.PyEmVue") as mock_vue_cls,
    ):
        mock_vue = MagicMock()
        mock_vue.customer = mock_customer
        mock_vue_cls.return_value = mock_vue

        info_default = await validate_input(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                ENABLE_1M: True,
                ENABLE_1D: True,
                ENABLE_1MON: True,
                SOLAR_INVERT: True,
            }
        )
        assert info_default[SYNC_CIRCUIT_NAMES] is True

        info_disabled = await validate_input(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                ENABLE_1M: True,
                ENABLE_1D: True,
                ENABLE_1MON: True,
                SOLAR_INVERT: True,
                SYNC_CIRCUIT_NAMES: False,
            }
        )
        assert info_disabled[SYNC_CIRCUIT_NAMES] is False

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD,
            CUSTOMER_GID: "12345",
            CONFIG_TITLE: "user@example.com (12345)",
            ENABLE_1M: True,
            ENABLE_1D: True,
            ENABLE_1MON: True,
            SOLAR_INVERT: True,
            SYNC_CIRCUIT_NAMES: True,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch.object(hass.config_entries, "async_reload", return_value=True):
        updated = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                ENABLE_1M: True,
                ENABLE_1D: True,
                ENABLE_1MON: True,
                SOLAR_INVERT: True,
                SYNC_CIRCUIT_NAMES: False,
            },
        )
        await hass.async_block_till_done()

    assert updated["type"] == FlowResultType.ABORT
    assert updated["reason"] == "reconfigure_successful"
    assert entry.data[SYNC_CIRCUIT_NAMES] is False


async def test_circuit_rename_sync_enabled_and_name_by_user_preserved(
    hass: HomeAssistant,
) -> None:
    """Test circuit rename propagation when enabled while preserving name_by_user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_rename_sync_on",
        data={SYNC_CIRCUIT_NAMES: True},
    )
    entry.add_to_hass(hass)
    devices = _build_test_devices()

    # Initial registration
    async_setup_device_registry(hass, entry, devices)
    dev_reg = dr.async_get(hass)

    ct1 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
    ct2 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-2"), entry.entry_id)
    ct3 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-3"), entry.entry_id)
    assert ct1 is not None and ct1.name == "Dishwasher"
    assert ct2 is not None and ct2.name == "Oven"
    assert ct3 is not None and ct3.name == "Guest Bedroom"

    # User customizes Oven in Home Assistant UI
    dev_reg.async_update_device(ct2.id, name_by_user="Custom Kitchen Oven")

    # Upstream Emporia app renames circuits and monitor, and clears ch3 name
    devices[1001].device_name = "Main Panel"
    devices[1001].channels[1].name = "Heat Pump Water Heater"
    devices[1001].channels[2].name = "App Renamed Oven"
    devices[1001].channels[3].name = None

    async_setup_device_registry(hass, entry, devices)

    phys_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    ct1_after = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1"), entry.entry_id
    )
    ct2_after = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-2"), entry.entry_id
    )
    ct3_after = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-3"), entry.entry_id
    )

    assert phys_dev is not None and phys_dev.name == "Main Panel"
    assert ct1_after is not None and ct1_after.name == "Heat Pump Water Heater"
    # User-customized device retains both its custom name_by_user and original registry name
    assert ct2_after is not None
    assert ct2_after.name_by_user == "Custom Kitchen Oven"
    assert ct2_after.name == "Oven"
    # Cleared circuit name falls back to translated circuit label
    assert ct3_after is not None and ct3_after.name == "Main Panel circuit 3"


async def test_circuit_rename_sync_disabled_preserves_existing_names(
    hass: HomeAssistant,
) -> None:
    """Test that disabling sync_circuit_names prevents overwriting existing device names."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_rename_sync_off",
        data={SYNC_CIRCUIT_NAMES: False},
    )
    entry.add_to_hass(hass)
    devices = _build_test_devices()

    # Initial registration still populates initial names
    async_setup_device_registry(hass, entry, devices)
    dev_reg = dr.async_get(hass)

    ct1 = dev_reg.async_get_device_by_identifier((DOMAIN, "1001-1"), entry.entry_id)
    assert ct1 is not None and ct1.name == "Dishwasher"

    # Upstream Emporia app renames monitor and circuit 1
    devices[1001].device_name = "Renamed Monitor"
    devices[1001].channels[1].name = "Renamed Circuit 1"

    async_setup_device_registry(hass, entry, devices)

    phys_after = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1,2,3"), entry.entry_id
    )
    ct1_after = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "1001-1"), entry.entry_id
    )
    assert phys_after is not None and phys_after.name == "Home Vue"
    assert ct1_after is not None and ct1_after.name == "Dishwasher"
    # In-memory model also kept aligned so entity platform setup does not overwrite registry
    assert devices[1001].device_name == "Home Vue"
    assert devices[1001].channels[1].name == "Dishwasher"


async def test_sse_stream_channel_rename_sync(hass: HomeAssistant) -> None:
    """Test reactive circuit rename updates arriving over SSE when sync_circuit_names is enabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SSE Rename Test",
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "password123",
            ENABLE_1M: False,
            ENABLE_1D: False,
            ENABLE_1MON: False,
            SOLAR_INVERT: True,
            SYNC_CIRCUIT_NAMES: True,
        },
        entry_id="sse_rename_entry",
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    devices = _build_test_devices()

    mock_vue = MagicMock()
    mock_vue.login.return_value = True
    mock_vue.get_devices_v1.return_value = list(devices.values())
    mock_vue.get_devices_status.return_value = ([], [])

    event_queue: asyncio.Queue[DeviceStatusDelta] = asyncio.Queue()

    async def fake_listen():
        while True:
            delta = await event_queue.get()
            yield delta
            event_queue.task_done()

    with (
        patch("custom_components.emporia_vue.PyEmVue", return_value=mock_vue),
        patch(
            "custom_components.emporia_vue.EmporiaStreamClient"
        ) as mock_stream_client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        mock_client = MagicMock(spec=EmporiaStreamClient)
        mock_client.listen.side_effect = fake_listen
        mock_client.close = AsyncMock()
        mock_stream_client_cls.return_value = mock_client

        assert await async_setup_entry(hass, entry)

        dev_reg = dr.async_get(hass)
        ct1_initial = dev_reg.async_get_device_by_identifier(
            (DOMAIN, "1001-1"), entry.entry_id
        )
        assert ct1_initial is not None and ct1_initial.name == "Dishwasher"
        assert hass.data[DOMAIN][entry.entry_id]["coordinator_device_status"] is None

        # Push an SSE delta renaming channel 1 and channel 2 (via Branch_2 channel_id)
        channel_rename_delta = DeviceStatusDelta.from_json_dictionary(
            "DEVICE_STATUS.DELTA",
            {
                "device_gid": 1001,
                "channels": [
                    {"channel_num": "1", "name": "Induction Range"},
                    {"channel_id": "Branch_2", "display_name": "Wall Oven"},
                ],
            },
        )
        await event_queue.put(channel_rename_delta)
        await asyncio.wait_for(event_queue.join(), timeout=1.0)

        ct1_updated = dev_reg.async_get_device_by_identifier(
            (DOMAIN, "1001-1"), entry.entry_id
        )
        ct2_updated = dev_reg.async_get_device_by_identifier(
            (DOMAIN, "1001-2"), entry.entry_id
        )
        assert ct1_updated is not None and ct1_updated.name == "Induction Range"
        assert ct2_updated is not None and ct2_updated.name == "Wall Oven"

        await async_unload_entry(hass, entry)
