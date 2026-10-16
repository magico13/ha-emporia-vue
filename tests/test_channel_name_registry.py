"""Verify name refresh against Home Assistant's real device registry."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from test_channel_names import helpers

SOURCE = Path(__file__).parents[1] / "custom_components/emporia_vue/__init__.py"


@pytest.mark.asyncio
async def test_registry_refresh_preserves_identity_and_user_override(hass):
    # Load the production helper without importing the cloud integration setup.
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_update_registered_channel_names"
    )
    namespace = {
        "HomeAssistant": object,
        "DOMAIN": "emporia_vue",
        "DEVICE_INFORMATION": {
            12: SimpleNamespace(
                device_name="Vue",
                channels=[
                    SimpleNamespace(channel_num="1", name="New kitchen"),
                    SimpleNamespace(channel_num="2", name="New garage"),
                ],
            )
        },
        "channel_display_name": helpers.channel_display_name,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    entry = MockConfigEntry(domain="emporia_vue")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    first = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("emporia_vue", "12-1")},
        name="Old kitchen",
    )
    second = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("emporia_vue", "12-2")},
        name="Old garage",
    )
    registry.async_update_device(second.id, name_by_user="My custom name")
    await namespace["async_update_registered_channel_names"](hass)
    await hass.async_block_till_done()
    renamed = registry.async_get(first.id)
    assert renamed.name == "New kitchen"
    assert renamed.identifiers == first.identifiers
    overridden = registry.async_get(second.id)
    assert overridden.name_by_user == "My custom name"
    assert overridden.name == "Old garage"
