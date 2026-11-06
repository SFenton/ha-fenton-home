"""``fenton_home/house_config`` and its subscription: auth, roles, validation, reload."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_integration import DATA
from custom_components.fenton_home.const import (
    DOMAIN,
    HOUSE_CONFIG_MAX_BYTES,
    ISSUE_HOUSE_CONFIG_INVALID,
    SERVICE_RELOAD_HOUSE_CONFIG,
    WS_HOUSE_CONFIG,
    WS_HOUSE_CONFIG_SUBSCRIBE,
)
from custom_components.fenton_home.house_config import get as house

DEMO = {"schema": 1, "rooms": [{"id": "demo_room", "name": "Demo Room"}]}
DEMO_2 = {"schema": 1, "rooms": [{"id": "demo_room", "name": "Demo Room"}, {"id": "demo_hall", "name": "Demo Hall"}]}


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Each test gets its own configuration folder, so files never leak between tests."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def folder(hass: HomeAssistant) -> Path:
    path = Path(hass.config.config_dir) / "fenton_home"
    path.mkdir(exist_ok=True)
    return path


async def start(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


async def write(hass: HomeAssistant, name: str, text: str) -> None:
    await hass.async_add_executor_job((folder(hass) / name).write_text, text)


async def reload(hass: HomeAssistant, user_id: str | None = None) -> None:
    await hass.services.async_call(DOMAIN, SERVICE_RELOAD_HOUSE_CONFIG, blocking=True,
                                   context=Context(user_id=user_id))


def invalid_issue(hass: HomeAssistant):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HOUSE_CONFIG_INVALID)


async def test_a_connection_needs_a_token(hass: HomeAssistant, hass_client_no_auth) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    client = await hass_client_no_auth()
    ws = await client.ws_connect("/api/websocket")
    assert (await ws.receive_json())["type"] == "auth_required"
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["type"] == "auth_invalid"


async def test_a_non_admin_user_can_read(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    ws = await hass_ws_client(access_token=hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    answer = await ws.receive_json()
    assert answer["success"] is True
    assert answer["result"]["schema"] == 1 and answer["result"]["config"] == {"rooms": DEMO["rooms"]}
    assert len(answer["result"]["hash"]) == 64


async def test_not_configured(hass: HomeAssistant, hass_ws_client) -> None:
    await start(hass)
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"] == {"configured": False}
    assert invalid_issue(hass) is None
    await ws.send_json({"id": 2, "type": WS_HOUSE_CONFIG_SUBSCRIBE})
    assert (await ws.receive_json())["success"] is True
    assert (await ws.receive_json())["event"] == {"configured": False}


async def test_yaml_wins_over_json(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await write(hass, "house.yaml", "schema: 7\nrooms: []\n")
    await start(hass)
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"]["schema"] == 7


async def test_an_invalid_file_raises_a_repair_and_keeps_the_last_good(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    good = (await ws.receive_json())["result"]

    await write(hass, "house.json", '{"schema": 0, "rooms": []}')
    await reload(hass)
    issue = invalid_issue(hass)
    assert issue is not None and issue.translation_placeholders["reason"] == "bad_schema"
    await ws.send_json({"id": 2, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"] == good

    await write(hass, "house.json", json.dumps(DEMO_2))
    await reload(hass)
    assert invalid_issue(hass) is None
    await ws.send_json({"id": 3, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"]["config"] == {"rooms": DEMO_2["rooms"]}


async def test_an_invalid_first_file_serves_not_configured(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", "{broken")
    await start(hass)
    assert invalid_issue(hass).translation_placeholders["reason"] == "unparseable"
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"] == {"configured": False}


async def test_the_size_limit(hass: HomeAssistant) -> None:
    await write(hass, "house.json", json.dumps({"schema": 1, "rooms": ["x" * 1024] * 300}))
    assert (folder(hass) / "house.json").stat().st_size > HOUSE_CONFIG_MAX_BYTES
    await start(hass)
    assert invalid_issue(hass).translation_placeholders["reason"] == "too_large"


async def test_reload_notifies_subscribers_and_unsubscribe_is_clean(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    ws = await hass_ws_client()
    await ws.send_json({"id": 5, "type": WS_HOUSE_CONFIG_SUBSCRIBE})
    assert (await ws.receive_json())["success"] is True
    first = (await ws.receive_json())["event"]
    assert first["config"] == {"rooms": DEMO["rooms"]}

    await write(hass, "house.json", json.dumps(DEMO_2))
    await reload(hass)
    second = await ws.receive_json()
    assert second["id"] == 5 and second["event"]["config"] == {"rooms": DEMO_2["rooms"]}
    assert second["event"]["hash"] != first["hash"]

    # A rejected file sends nothing.
    await write(hass, "house.json", "{broken")
    await reload(hass)
    await ws.send_json({"id": 6, "type": "unsubscribe_events", "subscription": 5})
    assert (await ws.receive_json())["success"] is True
    await write(hass, "house.json", json.dumps(DEMO))
    await reload(hass)
    await ws.send_json({"id": 7, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["id"] == 7  # no event for subscription 5 arrived in between


async def test_the_reload_service_is_admin_only(hass: HomeAssistant, hass_read_only_user, hass_admin_user) -> None:
    await start(hass)
    with pytest.raises(Unauthorized):
        await reload(hass, hass_read_only_user.id)
    await reload(hass, hass_admin_user.id)


async def test_a_deeply_nested_file_never_fails_setup(hass: HomeAssistant) -> None:
    await write(hass, "house.yaml", "schema: 1\nrooms: " + "[" * 1200)
    await start(hass)
    assert DOMAIN in hass.config.components
    assert invalid_issue(hass) is not None


async def test_an_unexpected_error_raises_a_repair_and_keeps_the_last_good(hass: HomeAssistant) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    good = house(hass).payload
    with patch("custom_components.fenton_home.house_config.load_file", side_effect=PermissionError("denied")):
        await reload(hass)
    assert invalid_issue(hass).translation_placeholders["reason"] == "unexpected"
    assert house(hass).payload == good


async def test_concurrent_reloads_finish_in_order(hass: HomeAssistant) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    await write(hass, "house.json", json.dumps(DEMO_2))
    await asyncio.gather(house(hass).async_reload(), house(hass).async_reload())
    assert house(hass).payload["config"] == {"rooms": DEMO_2["rooms"]}


async def test_a_disconnect_without_unsubscribing_leaks_no_listener(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG_SUBSCRIBE})
    await ws.receive_json()
    await ws.receive_json()
    assert len(house(hass)._listeners) == 1
    await ws.close()
    await hass.async_block_till_done()
    assert len(house(hass)._listeners) == 0


async def test_unloading_keeps_subscribers_and_setup_pushes_to_them(hass: HomeAssistant, hass_ws_client) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "websocket_api", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": WS_HOUSE_CONFIG_SUBSCRIBE})
    await ws.receive_json()
    assert (await ws.receive_json())["event"]["config"] == {"rooms": DEMO["rooms"]}

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (await ws.receive_json())["event"] == {"configured": False}
    assert len(house(hass)._listeners) == 1
    await ws.send_json({"id": 2, "type": WS_HOUSE_CONFIG})
    assert (await ws.receive_json())["result"] == {"configured": False}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    event = await ws.receive_json()
    assert event["id"] == 1 and event["event"]["config"] == {"rooms": DEMO["rooms"]}

    await write(hass, "house.json", json.dumps(DEMO_2))
    await reload(hass)
    assert (await ws.receive_json())["event"]["config"] == {"rooms": DEMO_2["rooms"]}

    await ws.close()
    await hass.async_block_till_done()
    assert len(house(hass)._listeners) == 0


async def test_an_unload_during_a_reload_leaves_no_stale_repair_or_document(hass: HomeAssistant) -> None:
    await write(hass, "house.json", json.dumps(DEMO))
    await start(hass)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_executor(func, *args):
        started.set()
        await release.wait()
        return func(*args)

    await write(hass, "house.json", "{broken")
    with patch.object(hass, "async_add_executor_job", slow_executor):
        task = hass.async_create_task(house(hass).async_reload())
        await started.wait()
        house(hass).async_deactivate()
        release.set()
        assert await task is False
    assert invalid_issue(hass) is None
    assert house(hass).document is None
    assert house(hass).payload == {"configured": False}
