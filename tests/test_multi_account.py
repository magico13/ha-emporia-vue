"""Tests for multi-account Emporia Vue config entries and lifecycle isolation."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pyemvue.device import (
    ChargerDevice,
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue import async_setup_entry, async_unload_entry
from custom_components.emporia_vue.const import (
    AUTH_METHOD,
    AUTH_METHOD_EMAIL_PASSWORD,
    AUTH_METHOD_TOKENS,
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CUSTOMER_GID,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
)

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "emporia_vue"
)


def _mock_validate_result(email: str, customer_gid: str) -> dict[str, object]:
    return {
        "title": f"{email} ({customer_gid})",
        CUSTOMER_GID: customer_gid,
        ENABLE_1M: True,
        ENABLE_1D: True,
        ENABLE_1MON: True,
        "solar_invert": True,
        AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD,
        CONF_EMAIL: email,
        CONF_PASSWORD: "secret",
    }


def _make_vue_device_and_client(
    device_gid: int,
    *,
    minute_usage: float,
    day_usage: float,
    month_usage: float,
) -> tuple[MagicMock, VueDevice, ChargerDevice]:
    """Build a mock PyEmVue client with one solar channel and one EV charger."""
    channel = VueDeviceChannel(
        gid=device_gid,
        name="Solar",
        channelNum="1,2,3",
        channelMultiplier=1.0,
        channelTypeGid=13,
    )
    channel.type = "Main"

    charger = ChargerDevice(gid=device_gid)
    charger.charger_on = True
    charger.charging_rate = 16
    charger.max_charging_rate = 40

    device = VueDevice(gid=device_gid, manId="Emporia", modelNum="VUE2")
    device.device_name = f"Panel {device_gid}"
    device.time_zone = "UTC"
    device.billing_cycle_start_day = 1
    device.channels = [channel]
    device.outlet = None
    device.ev_charger = charger

    def _get_device_list_usage(
        device_gids: list[str],
        instant: datetime,
        scale: str,
    ) -> dict[int, VueUsageDevice]:
        # Strictly verify only this account's device_gid is requested
        assert device_gids == [str(device_gid)]
        usage_val = {
            Scale.MINUTE.value: minute_usage,
            Scale.DAY.value: day_usage,
            Scale.MONTH.value: month_usage,
        }[scale]
        usage_dev = VueUsageDevice(gid=device_gid, timestamp=instant or datetime.now(UTC))
        ch_usage = VueDeviceChannelUsage(
            gid=device_gid,
            usage=usage_val,
            channelNum="1,2,3",
            name="Solar",
        )
        usage_dev.channels = {"1,2,3": ch_usage}
        return {device_gid: usage_dev}

    def _update_charger(
        charger_obj: ChargerDevice,
        on: bool,
        rate: int,
    ) -> ChargerDevice:
        updated = ChargerDevice(gid=charger_obj.device_gid)
        updated.charger_on = on
        updated.charging_rate = rate
        updated.max_charging_rate = charger_obj.max_charging_rate
        return updated

    vue = MagicMock(
        spec=[
            "login",
            "get_devices",
            "get_devices_status",
            "get_device_list_usage",
            "update_charger",
            "auth",
        ]
    )
    vue.auth = None
    vue.login.return_value = True
    vue.get_devices.return_value = [device]
    vue.get_devices_status.return_value = ([], [charger])
    vue.get_device_list_usage.side_effect = _get_device_list_usage
    vue.update_charger.side_effect = _update_charger
    return vue, device, charger


def test_manifest_and_translations_support_multi_account() -> None:
    """Verify manifest allows multiple entries and translations use Add account."""
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
    assert manifest.get("single_config_entry") is not True

    strings = json.loads((COMPONENT_DIR / "strings.json").read_text())
    assert (
        strings["config"]["initiate_flow"]["user"]
        == "[%key:common::config_flow::initiate_flow::account%]"
    )
    assert (
        strings["config"]["abort"]["already_configured"]
        == "[%key:common::config_flow::abort::already_configured_account%]"
    )

    en = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())
    assert en["config"]["initiate_flow"]["user"] == "Add account"
    assert en["config"]["abort"]["already_configured"] == "Account is already configured"


async def test_multi_account_config_flow_creates_separate_entries_and_aborts_duplicate(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Two distinct Emporia accounts can be added; duplicate customer_gid aborts."""
    with (
        patch(
            "custom_components.emporia_vue.config_flow.validate_input",
            new=AsyncMock(
                return_value=_mock_validate_result("user1@example.com", "111")
            ),
        ),
        patch(
            "custom_components.emporia_vue.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.emporia_vue.async_unload_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result1 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result1["type"] is FlowResultType.FORM
        result1 = await hass.config_entries.flow.async_configure(
            result1["flow_id"], {AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD}
        )
        assert result1["step_id"] == "email_password"
        result1 = await hass.config_entries.flow.async_configure(
            result1["flow_id"],
            {CONF_EMAIL: "user1@example.com", CONF_PASSWORD: "secret"},
        )
        assert result1["type"] is FlowResultType.CREATE_ENTRY
        assert result1["title"] == "user1@example.com (111)"
        assert result1["result"].unique_id == "111"

    with (
        patch(
            "custom_components.emporia_vue.config_flow.validate_input",
            new=AsyncMock(
                return_value=_mock_validate_result("user2@example.com", "222")
            ),
        ),
        patch(
            "custom_components.emporia_vue.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.emporia_vue.async_unload_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result2["flow_id"], {AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {CONF_EMAIL: "user2@example.com", CONF_PASSWORD: "secret"},
        )
        assert result2["type"] is FlowResultType.CREATE_ENTRY
        assert result2["title"] == "user2@example.com (222)"
        assert result2["result"].unique_id == "222"

    # Re-adding customer_gid 111 via email_password must abort as already_configured
    with patch(
        "custom_components.emporia_vue.config_flow.validate_input",
        new=AsyncMock(return_value=_mock_validate_result("user1@example.com", "111")),
    ):
        dup = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        dup = await hass.config_entries.flow.async_configure(
            dup["flow_id"], {AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD}
        )
        dup = await hass.config_entries.flow.async_configure(
            dup["flow_id"],
            {CONF_EMAIL: "user1@example.com", CONF_PASSWORD: "secret"},
        )
        assert dup["type"] is FlowResultType.ABORT
        assert dup["reason"] == "already_configured"

    # Re-adding customer_gid 222 via tokens must also abort as already_configured
    with patch(
        "custom_components.emporia_vue.config_flow.validate_input",
        new=AsyncMock(return_value=_mock_validate_result("user2@example.com", "222")),
    ):
        dup_tok = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        dup_tok = await hass.config_entries.flow.async_configure(
            dup_tok["flow_id"], {AUTH_METHOD: AUTH_METHOD_TOKENS}
        )
        assert dup_tok["step_id"] == "tokens"
        dup_tok = await hass.config_entries.flow.async_configure(
            dup_tok["flow_id"],
            {
                CONF_ID_TOKEN: "id-tok",
                CONF_ACCESS_TOKEN: "acc-tok",
                CONF_REFRESH_TOKEN: "ref-tok",
            },
        )
        assert dup_tok["type"] is FlowResultType.ABORT
        assert dup_tok["reason"] == "already_configured"


async def test_concurrent_entries_isolate_runtime_state_and_charger_service(
    hass: HomeAssistant,
) -> None:
    """Two active entries isolate device GIDs, solar_invert, day/month caches, and charger service."""
    vue1, _, _ = _make_vue_device_and_client(
        101, minute_usage=-0.5, day_usage=-12.0, month_usage=-150.0
    )
    vue2, _, _ = _make_vue_device_and_client(
        202, minute_usage=-0.8, day_usage=-20.0, month_usage=-300.0
    )

    entry1 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="111",
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        data={
            CUSTOMER_GID: "111",
            CONF_EMAIL: "user1@example.com",
            CONF_PASSWORD: "secret",
            AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD,
            ENABLE_1M: True,
            ENABLE_1D: True,
            ENABLE_1MON: True,
            SOLAR_INVERT: True,
        },
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="222",
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        data={
            CUSTOMER_GID: "222",
            CONF_EMAIL: "user2@example.com",
            CONF_PASSWORD: "secret",
            AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD,
            ENABLE_1M: True,
            ENABLE_1D: True,
            ENABLE_1MON: True,
            SOLAR_INVERT: False,
        },
    )
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_unload",
            new=AsyncMock(return_value=True),
        ),
    ):
        with patch("custom_components.emporia_vue.PyEmVue", return_value=vue1):
            assert await async_setup_entry(hass, entry1) is True

        with patch("custom_components.emporia_vue.PyEmVue", return_value=vue2):
            assert await async_setup_entry(hass, entry2) is True

        store1 = hass.data[DOMAIN][entry1.entry_id]
        store2 = hass.data[DOMAIN][entry2.entry_id]

        # 1. Both entries must have fetched their own initial 1min, 1day, and 1mon data
        #    and applied their respective per-entry SOLAR_INVERT setting.
        assert store1["coordinator_1min"].data["101-1,2,3-1MIN"]["usage"] == 0.5
        assert store1["coordinator_day_sensor"].data["101-1,2,3-1D"]["usage"] == 12.0
        assert store1["coordinator_1mon"].data["101-1,2,3-1MON"]["usage"] == 150.0
        assert "202-1,2,3-1MIN" not in store1["coordinator_1min"].data

        assert store2["coordinator_1min"].data["202-1,2,3-1MIN"]["usage"] == -0.8
        assert store2["coordinator_day_sensor"].data["202-1,2,3-1D"]["usage"] == -20.0
        assert store2["coordinator_1mon"].data["202-1,2,3-1MON"]["usage"] == -300.0
        assert "101-1,2,3-1MIN" not in store2["coordinator_1min"].data

        # 2. Refreshing Account 1's 1min coordinator after Account 2 is loaded must
        #    poll Account 1's device_gid ("101") and preserve Account 1's SOLAR_INVERT=True.
        await store1["coordinator_1min"].async_refresh()
        assert store1["coordinator_1min"].data["101-1,2,3-1MIN"]["usage"] == 0.5

        # 3. Calling set_charger_current for Account 1's charger while Account 2 is loaded
        #    must route to Account 1's vue1 client, not vue2.
        entity_reg = er.async_get(hass)
        reg_entry1 = entity_reg.async_get_or_create(
            "switch",
            DOMAIN,
            "charger-101",
            suggested_object_id="charger_101",
            config_entry=entry1,
        )
        hass.states.async_set(reg_entry1.entity_id, "on", {"charging_rate": 16})

        await hass.services.async_call(
            DOMAIN,
            "set_charger_current",
            {"entity_id": reg_entry1.entity_id, "current": 24},
            blocking=True,
        )
        assert vue1.update_charger.call_count == 1
        assert vue2.update_charger.call_count == 0
        assert hass.states.get(reg_entry1.entity_id).attributes["charging_rate"] == 24

        # 4. Unloading Account 2 keeps Account 1's data and service active
        assert await async_unload_entry(hass, entry2) is True
        assert entry2.entry_id not in hass.data[DOMAIN]
        assert entry1.entry_id in hass.data[DOMAIN]
        assert hass.services.has_service(DOMAIN, "set_charger_current")

        await hass.services.async_call(
            DOMAIN,
            "set_charger_current",
            {"entity_id": reg_entry1.entity_id, "current": 32},
            blocking=True,
        )
        assert vue1.update_charger.call_count == 2
        assert hass.states.get(reg_entry1.entity_id).attributes["charging_rate"] == 32

        # 5. Unloading the last entry removes the service
        assert await async_unload_entry(hass, entry1) is True
        assert not hass.data[DOMAIN]
        assert not hass.services.has_service(DOMAIN, "set_charger_current")
