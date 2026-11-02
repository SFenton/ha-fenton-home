"""The 1.0 rename: import of a leftover fenton_push entry and store, the old paths and service, and their Repairs."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fenton_home.const import (
    CONF_CRITICAL_ALERTS,
    CONF_LIVE_JOBS,
    CONF_PUSH_BASE_URL,
    DOMAIN,
    ISSUE_ALIASES_CAN_GO,
    ISSUE_LEGACY_SERVICE,
)
from custom_components.fenton_home.legacy import async_import_legacy, async_migrate_store

from .. import samples
from .test_integration import DATA, PUSH_URL, STEPHEN, body, mobile_app_actions, mobile_app_entry

OLD_PUSH_URL = PUSH_URL.replace("fenton_home", "fenton_push")
OPTIONS = {CONF_CRITICAL_ALERTS: True, CONF_PUSH_BASE_URL: "http://10.0.0.5:8123", CONF_LIVE_JOBS: ["fan"]}
CARDS = {"cards": [{"webhook_id": "hook-stephen-iphone", "tag": "job.fan.guest", "started_at": 1.0}], "records": []}


def old_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The pre-1.0 entry: its integration files are gone, so it never loads."""
    entry = MockConfigEntry(domain="fenton_push", data=DATA, options=OPTIONS, unique_id="com.sfenton.home",
                            title="com.sfenton.home (key ABC123DEFG)")
    entry.add_to_hass(hass)
    return entry


def new_entries(hass: HomeAssistant) -> list[MockConfigEntry]:
    return hass.config_entries.async_entries(DOMAIN)


# -- import -----------------------------------------------------------------------------------------------------------


async def test_import_copies_data_and_options_removes_the_old_entry_and_moves_the_store(
        hass: HomeAssistant, hass_storage, caplog) -> None:
    assert await async_setup_component(hass, "http", {})
    old = old_entry(hass)
    hass_storage["fenton_push.live_jobs"] = {"version": 1, "minor_version": 1, "key": "fenton_push.live_jobs",
                                              "data": CARDS}
    await async_import_legacy(hass)
    await hass.async_block_till_done()

    [entry] = new_entries(hass)
    assert entry.data == DATA and entry.options == OPTIONS and entry.unique_id == "com.sfenton.home"
    assert entry.source == config_entries.SOURCE_IMPORT
    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert not hass.config_entries.async_entries("fenton_push")
    # The new entry's job engine loaded the moved store (and may have saved over it since).
    assert "fenton_home.live_jobs" in hass_storage
    assert "fenton_push.live_jobs" not in hass_storage
    # Neither the key nor any option value reaches the log.
    assert DATA["key"] not in caplog.text and "10.0.0.5" not in caplog.text


async def test_store_moves_before_the_new_entry_loads(hass: HomeAssistant, hass_storage) -> None:
    """The job engine of the imported entry reads the moved store (here it ends the orphaned card)."""
    hass_storage["fenton_push.live_jobs"] = {"version": 1, "minor_version": 1, "key": "fenton_push.live_jobs",
                                              "data": CARDS}
    assert await async_migrate_store(hass) is True
    assert hass_storage["fenton_home.live_jobs"]["data"] == CARDS
    assert "fenton_push.live_jobs" not in hass_storage


async def test_import_is_idempotent(hass: HomeAssistant, hass_storage) -> None:
    assert await async_setup_component(hass, "http", {})
    old_entry(hass)
    hass_storage["fenton_push.live_jobs"] = {"version": 1, "minor_version": 1, "key": "fenton_push.live_jobs",
                                              "data": CARDS}
    await async_import_legacy(hass)
    await hass.async_block_till_done()
    [first] = new_entries(hass)
    hass.config_entries.async_update_entry(first, options={**OPTIONS, CONF_LIVE_JOBS: ["washer"]})
    await async_import_legacy(hass)
    await async_import_legacy(hass)
    await hass.async_block_till_done()
    [again] = new_entries(hass)
    assert again.entry_id == first.entry_id and again.options[CONF_LIVE_JOBS] == ["washer"]
    assert await async_migrate_store(hass) is False


async def test_stale_old_entry_is_removed_when_the_new_one_exists(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    new = MockConfigEntry(domain=DOMAIN, data=DATA, options={}, unique_id="com.sfenton.home")
    new.add_to_hass(hass)
    old = old_entry(hass)
    await async_import_legacy(hass)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert new_entries(hass) == [new] and new.options == {}


async def test_nothing_to_import(hass: HomeAssistant, hass_storage) -> None:
    await async_import_legacy(hass)
    assert not new_entries(hass) and not hass_storage


async def test_domain_setup_starts_the_import(hass: HomeAssistant) -> None:
    """``fenton_home:`` in configuration.yaml (or any setup of the domain) imports the leftover entry."""
    assert await async_setup_component(hass, "http", {})
    old = old_entry(hass)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    assert len(new_entries(hass)) == 1 and hass.config_entries.async_get_entry(old.entry_id) is None


async def test_adding_the_integration_by_hand_takes_the_old_key(hass: HomeAssistant, hass_storage) -> None:
    """With no YAML line the domain never loads by itself; the Add integration flow does the import without a form."""
    assert await async_setup_component(hass, "http", {})
    old = old_entry(hass)
    hass_storage["fenton_push.live_jobs"] = {"version": 1, "minor_version": 1, "key": "fenton_push.live_jobs",
                                              "data": CARDS}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == DATA and result["options"] == OPTIONS
    await hass.async_block_till_done()
    assert len(new_entries(hass)) == 1 and hass.config_entries.async_get_entry(old.entry_id) is None
    assert "fenton_push.live_jobs" not in hass_storage


async def test_a_failed_import_keeps_the_old_entry(hass: HomeAssistant) -> None:
    old = old_entry(hass)
    with patch.object(hass.config_entries.flow, "async_init", AsyncMock(return_value={
            "type": FlowResultType.ABORT, "reason": "boom"})):
        await async_import_legacy(hass)
    assert hass.config_entries.async_get_entry(old.entry_id) is old and not new_entries(hass)


# -- aliases ----------------------------------------------------------------------------------------------------------


@pytest.fixture
async def loaded(hass: HomeAssistant):
    assert await async_setup_component(hass, "http", {})
    hass.states.async_set("person.stephen_fenton", "home", {"user_id": STEPHEN, "friendly_name": "Stephen Fenton"})
    mobile_app_entry(hass, "hook-stephen-iphone", STEPHEN)
    await mobile_app_actions(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_both_config_paths_answer_the_new_push_url(hass: HomeAssistant, loaded, hass_client) -> None:
    client = await hass_client()
    new = await (await client.get("/api/fenton_home/config")).json()
    old = await (await client.get("/api/fenton_push/config")).json()
    assert new == old and new["available"] is True
    assert new["push_url"].endswith("/api/fenton_home/apns") and "fenton_push" not in new["push_url"]


async def test_both_push_paths_forward(hass: HomeAssistant, loaded, hass_client) -> None:
    client = await hass_client()
    with patch("custom_components.fenton_home.apns.APNsClient.send", AsyncMock(return_value=None)) as send:
        for path in ("/api/fenton_home/apns", "/api/fenton_push/apns"):
            response = await client.post(path, json=body(sample=samples.FRONT_DOOR_UNSECURED))
            assert response.status == HTTPStatus.CREATED, path
        assert send.await_count == 2
        refused = await client.post("/api/fenton_push/apns", json=body(token="wrong"))
        assert refused.status == HTTPStatus.FORBIDDEN


async def test_the_old_problem_service_works_and_raises_a_repair(hass: HomeAssistant, loaded) -> None:
    assert hass.services.has_service("fenton_push", "problem") and hass.services.has_service("fenton_home", "problem")
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_LEGACY_SERVICE) is None
    handled = []
    runtime = hass.data[DOMAIN][loaded.entry_id]
    original = runtime.live.async_problem

    async def spy(data):
        handled.append(data)
        await original(data)

    runtime.live.async_problem = spy
    await hass.services.async_call("fenton_push", "problem", {"device": "main_floor", "code": "navigation.stuck"},
                                   blocking=True)
    assert [d["device"] for d in handled] == ["main_floor"]
    issue = registry.async_get_issue(DOMAIN, ISSUE_LEGACY_SERVICE)
    assert issue is not None and issue.translation_placeholders == {"old": "fenton_push.problem",
                                                                    "new": "fenton_home.problem"}
    # The new service does not raise it.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_SERVICE)
    await hass.services.async_call("fenton_home", "problem", {"device": "main_floor", "code": "navigation.stuck"},
                                   blocking=True)
    assert registry.async_get_issue(DOMAIN, ISSUE_LEGACY_SERVICE) is None
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert not hass.services.has_service("fenton_push", "problem")


# -- "the old paths can go" -------------------------------------------------------------------------------------------


def can_go(hass: HomeAssistant) -> bool:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ALIASES_CAN_GO) is not None


async def test_repair_appears_once_every_registration_uses_the_new_path(hass: HomeAssistant, loaded) -> None:
    # The fixture's registration already posts to the new path; an old-path one holds the issue back.
    assert can_go(hass)
    stale = mobile_app_entry(hass, "hook-stephanie-iphone", "user-stephanie",
                             app_data={"push_url": OLD_PUSH_URL, "push_token": "t"})
    hass.config_entries.async_update_entry(stale, title="touch")  # a registration changing fires the check
    await hass.async_block_till_done()
    assert not can_go(hass)
    hass.config_entries.async_update_entry(stale, data={
        **stale.data, "app_data": {"push_url": PUSH_URL, "push_token": "t"}})
    await hass.async_block_till_done()
    assert can_go(hass)
    # Signed-out registrations (no push_url) and other apps' registrations don't count.
    mobile_app_entry(hass, "hook-out", STEPHEN, app_data={})
    mobile_app_entry(hass, "hook-companion", STEPHEN, app_id="io.robbie.HomeAssistant",
                     app_data={"push_url": "https://mobile-apps.home-assistant.io/api/sendPushNotification"})
    hass.config_entries.async_update_entry(stale, title="touch again")
    await hass.async_block_till_done()
    assert can_go(hass)


async def test_no_repair_while_no_registration_has_a_push_url(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    mobile_app_entry(hass, "hook-out", STEPHEN, app_data={})
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not can_go(hass)
