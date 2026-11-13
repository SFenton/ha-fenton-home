"""Dead Live Activity tokens (410) and the per-activity timestamps, through the push endpoint."""

from __future__ import annotations

import copy
import sys
import types
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.fenton_home import recovery
from custom_components.fenton_home.apns import APNsError

from .. import samples
from .test_integration import CLIENT, body
from .test_integration import configured as configured  # noqa: F401  (a fixture, used by name)

START_TOKEN = "d" * 64
REMOVE = "custom_components.fenton_home.views.recovery.remove_token"


@pytest.fixture
async def device(hass: HomeAssistant, configured):  # noqa: F811
    """Stephen's iPhone, with a push-to-start token."""
    entry = next(e for e in hass.config_entries.async_entries("mobile_app") if e.data["webhook_id"] == "hook-stephen-iphone")
    hass.config_entries.async_update_entry(entry, data={
        **entry.data, "app_data": {**entry.data["app_data"], "start_live_activity_token": START_TOKEN}})
    return entry


def update_body() -> dict:
    return body(sample=copy.deepcopy(samples.LIVE_UPDATE))


async def post(client, payload: dict):
    return await client.post("/api/fenton_home/apns", json=payload)


async def test_a_dead_update_token_is_removed_and_the_card_starts_again(hass, device, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=[APNsError(410, "Unregistered"), None])) as send, \
         patch(REMOVE, MagicMock(return_value=True)) as remove:
        response = await post(client, update_body())
    assert response.status == HTTPStatus.CREATED
    remove.assert_called_once_with(hass, "hook-stephen-iphone", "fenton-home")
    (first_token, first, _), (second_token, second, _) = (call.args for call in send.await_args_list)
    assert first_token == "c" * 64 and first.payload["aps"]["event"] == "update"
    assert second_token == START_TOKEN and second.payload["aps"]["event"] == "start"
    assert second.payload["aps"]["attributes-type"] == "HomeActivityAttributes" and second.priority == 10
    assert second.payload["aps"]["timestamp"] > first.payload["aps"]["timestamp"]
    assert (await response.json())["rateLimits"]["successful"] == 1


async def test_an_end_that_finds_the_activity_gone_is_a_success(hass, device, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    ending = body(sample=copy.deepcopy(samples.LIVE_END))
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))) as send, \
         patch(REMOVE, MagicMock(return_value=True)) as remove:
        response = await post(client, ending)
    assert response.status == HTTPStatus.CREATED
    assert send.await_count == 1
    remove.assert_called_once_with(hass, "hook-stephen-iphone", "fenton-home")


async def test_without_a_start_token_the_410_is_reported(hass, configured, hass_client_no_auth) -> None:  # noqa: F811
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))) as send, \
         patch(REMOVE, MagicMock(return_value=True)) as remove:
        response = await post(client, update_body())
    assert response.status == HTTPStatus.GONE and send.await_count == 1
    remove.assert_called_once()


async def test_an_update_with_no_activity_token_and_no_start_token_is_dropped_not_failed(
        hass, configured, hass_client_no_auth) -> None:  # noqa: F811
    """The app is between launches: core has no update token for the tag and no start token, so it posts the notify
    without ``live_activity_token``. The relay answers 201 and sends nothing; the engine keeps its card."""
    client = await hass_client_no_auth()
    update = update_body()
    update.pop("live_activity_token")
    with patch(f"{CLIENT}.send", AsyncMock()) as send, patch(REMOVE, MagicMock(return_value=True)) as remove:
        response = await post(client, update)
    assert response.status == HTTPStatus.CREATED and send.await_count == 0
    remove.assert_not_called()
    assert (await response.json())["rateLimits"]["successful"] == 0


async def test_a_retry_that_fails_too_is_reported(hass, device, hass_client_no_auth, caplog) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))) as send, \
         patch(REMOVE, MagicMock(return_value=True)):
        response = await post(client, update_body())
    assert response.status == HTTPStatus.GONE and send.await_count == 2
    assert "Couldn't restart" in caplog.text


async def test_a_dead_push_to_start_token_is_not_something_to_recover(hass, device, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    start = body(sample=copy.deepcopy(samples.LIVE_START))
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))) as send, \
         patch(REMOVE, MagicMock(return_value=True)) as remove:
        response = await post(client, start)
    assert response.status == HTTPStatus.GONE and send.await_count == 1
    remove.assert_called_once()  # the tag's stale token, if any, still goes
    ordinary = body()
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))), patch(REMOVE) as remove:
        assert (await post(client, ordinary)).status == HTTPStatus.GONE
    remove.assert_not_called()


async def test_timestamps_for_one_activity_never_tie(hass, device, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    stamps = []
    with patch(f"{CLIENT}.send", AsyncMock(return_value=None)) as send:
        for _ in range(3):
            assert (await post(client, update_body())).status == HTTPStatus.CREATED
        stamps = [call.args[1].payload["aps"]["timestamp"] for call in send.await_args_list]
        other = body("hook-stephen-ipad", sample=copy.deepcopy(samples.LIVE_UPDATE))
        await post(client, other)
        other_stamp = send.await_args_list[-1].args[1].payload["aps"]["timestamp"]
    assert stamps == sorted(set(stamps)) and len(stamps) == 3
    assert other_stamp <= stamps[-1], "another device has its own sequence"


# -- recovery.py ------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_warnings():
    recovery._warned.clear()
    yield
    recovery._warned.clear()


def fake_core(monkeypatch, *, remove=None, tokens=None, version="2026.10.1") -> MagicMock:
    remove = remove or MagicMock()
    store = types.ModuleType(recovery.STORE_MODULE)
    store.remove_live_activity_token = remove
    const = types.ModuleType(recovery.CONST_MODULE)
    const.DOMAIN = "mobile_app"
    const.DATA_LIVE_ACTIVITY_TOKENS = "live_activity_tokens"
    monkeypatch.setitem(sys.modules, recovery.STORE_MODULE, store)
    monkeypatch.setitem(sys.modules, recovery.CONST_MODULE, const)
    monkeypatch.setattr(recovery, "HA_VERSION", version)
    return remove


def test_remove_token_calls_cores_helper(hass: HomeAssistant, monkeypatch) -> None:
    remove = fake_core(monkeypatch, remove=lambda hass_, webhook_id, tag: calls.append((webhook_id, tag)))
    calls: list = []
    assert recovery.remove_token(hass, "hook", "job.fan.guest") is True
    assert calls == [("hook", "job.fan.guest")] and remove


def test_an_old_home_assistant_has_no_store_and_says_so_once(hass: HomeAssistant, monkeypatch, caplog) -> None:
    fake_core(monkeypatch, version="2026.6.4")
    assert recovery.remove_token(hass, "hook", "t") is False and recovery.remove_token(hass, "hook", "t") is False
    assert recovery.has_token(hass, "hook", "t") is None
    assert caplog.text.count("predates Live Activities") == 1


def test_a_changed_core_is_survived(hass: HomeAssistant, monkeypatch, caplog) -> None:
    fake_core(monkeypatch, remove=lambda a, b: None)  # a different signature
    assert recovery.remove_token(hass, "hook", "t") is False
    assert "remove_live_activity_token takes" in caplog.text
    fake_core(monkeypatch, remove=MagicMock(side_effect=KeyError("moved")))
    assert recovery.remove_token(hass, "hook", "t") is False
    monkeypatch.delitem(sys.modules, recovery.STORE_MODULE)
    monkeypatch.setattr(recovery, "STORE_MODULE", "homeassistant.components.mobile_app.nothing_here")
    assert recovery.remove_token(hass, "hook", "t") is False


def test_has_token_reads_cores_store(hass: HomeAssistant, monkeypatch) -> None:
    fake_core(monkeypatch)
    assert recovery.has_token(hass, "hook", "t") is None, "the store isn't there in this Home Assistant"
    hass.data["mobile_app"] = {"live_activity_tokens": {"hook": {"t": {"token": "x"}}}}
    assert recovery.has_token(hass, "hook", "t") is True
    assert recovery.has_token(hass, "hook", "other") is False and recovery.has_token(hass, "nobody", "t") is False


def test_versions() -> None:
    assert recovery._version() >= (2026, 1)
