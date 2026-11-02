"""Presence auto-enrolment (1.2): composites follow the registrations, people follow only their composite."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components import person
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_fire_time_changed,
    mock_integration,
)

from custom_components.fenton_home.const import (
    CONF_PRESENCE_DRY_RUN,
    CONF_PRESENCE_ENABLED,
    CONF_PRESENCE_EXCLUDE,
    DEFAULT_PRESENCE_EXCLUDE,
    DOMAIN,
    ISSUE_PRESENCE_COMPOSITE_MISSING,
    ISSUE_PRESENCE_COMPOSITE_YAML,
    ISSUE_PRESENCE_NO_COMPOSITE,
)
from custom_components.fenton_home.presence import Presence, async_plan

from .test_integration import configured, mobile_app_entry  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "presence-real-2026-11-02.json"
ON = {CONF_PRESENCE_ENABLED: True, CONF_PRESENCE_DRY_RUN: False}
DRY = {CONF_PRESENCE_ENABLED: True, CONF_PRESENCE_DRY_RUN: True}


class World:
    """A small house: two people (real ``person`` component), their registrations and Composite entries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.users: dict[str, str] = {}
        self.registrations: dict[str, MockConfigEntry] = {}
        self.composites: dict[str, MockConfigEntry] = {}

    async def user(self, key: str) -> str:
        if key not in self.users:
            self.users[key] = (await self.hass.auth.async_create_user(key)).id
        return self.users[key]

    def tracker(self, entity_id: str, owner: str, *, platform: str = "mobile_app", title: str | None = None) -> None:
        """A tracker of ``owner``'s registration (or of another integration when ``platform`` says so)."""
        uid = self.users.get(owner, owner)
        if platform == "mobile_app":
            entry = MockConfigEntry(domain="mobile_app", title=title or entity_id, data={"webhook_id": entity_id,
                                    "user_id": uid, "app_id": "io.robbie.HomeAssistant", "device_name": entity_id})
            entry.add_to_hass(self.hass)
            self.registrations[entity_id] = entry
            er.async_get(self.hass).async_get_or_create(
                "device_tracker", "mobile_app", f"uid-{entity_id}", config_entry=entry,
                suggested_object_id=entity_id.split(".", 1)[1])
        else:
            er.async_get(self.hass).async_get_or_create("device_tracker", platform, f"uid-{entity_id}",
                                                        suggested_object_id=entity_id.split(".", 1)[1])
        self.hass.states.async_set(entity_id, "home")

    def composite(self, entity_id: str, inputs: list[str], *, source: str = SOURCE_USER) -> MockConfigEntry:
        entry = MockConfigEntry(domain="composite", source=source, title=entity_id, data={},
                                options={"entity_id": [{"entity": i, "all_states": False, "use_picture": False}
                                                       for i in inputs], "require_movement": True})
        entry.add_to_hass(self.hass)
        er.async_get(self.hass).async_get_or_create("device_tracker", "composite", f"uid-{entity_id}",
                                                    config_entry=entry, suggested_object_id=entity_id.split(".", 1)[1])
        self.hass.states.async_set(entity_id, "home")
        self.composites[entity_id] = entry
        return entry

    async def person(self, name: str, owner: str, trackers: list[str]) -> str:
        await person.async_create_person(self.hass, name, user_id=await self.user(owner), device_trackers=trackers)
        return self.hass.data["person"][1].async_items()[-1]["id"]

    def person_trackers(self, pid: str) -> list[str]:
        return list(self.hass.data["person"][1].data[pid]["device_trackers"])

    def inputs(self, composite: str) -> list[str]:
        return [c["entity"] for c in self.composites[composite].options["entity_id"]]


@pytest.fixture
async def world(hass: HomeAssistant) -> World:
    mock_integration(hass, MockModule("composite"))
    assert await async_setup_component(hass, "person", {})
    return World(hass)


def presence(hass: HomeAssistant, **options) -> Presence:
    return Presence(hass, SimpleNamespace(options=options, presence=None))


async def house(world: World, *, source: str = SOURCE_USER) -> str:
    """Stephen: Duo and 18 Pro-like S+H phone in his composite; Stephanie has hers. Returns Stephen's person id."""
    await world.user("stephen")
    await world.user("stephanie")
    world.tracker("device_tracker.duo", "stephen")
    world.tracker("device_tracker.phone", "stephen")
    world.tracker("device_tracker.steph_phone", "stephanie")
    world.composite("device_tracker.stephen_location", ["device_tracker.duo", "device_tracker.phone"], source=source)
    world.composite("device_tracker.stephanie_location", ["device_tracker.steph_phone"], source=source)
    pid = await world.person("Stephen", "stephen", ["device_tracker.stephen_location"])
    await world.person("Stephanie", "stephanie", ["device_tracker.stephanie_location"])
    return pid


# -- the real server, today -------------------------------------------------------------------------------------------


def _real_house(world: World, *, unknown_owner: str) -> dict[str, str]:
    """The 2026-11-02 server (``fixtures/presence-real-2026-11-02.json``), read over the websocket."""
    data = json.loads(FIXTURE.read_text())
    owners = {"user-stephen": "stephen", "user-stephanie": "stephanie", "UNKNOWN": unknown_owner}
    for t in data["mobile_app"]:
        world.tracker(t["entity_id"], owners[t["user_id"]], title=t["title"])
    return {c["entity_id"]: c["inputs"] for c in data["composites"]}


async def _build_real(hass: HomeAssistant, world: World, *, unknown_owner: str, source: str) -> dict:
    await world.user("stephen")
    await world.user("stephanie")
    composites = _real_house(world, unknown_owner=unknown_owner)
    for entity_id, inputs in composites.items():
        world.composite(entity_id, inputs, source=source)
    data = json.loads(FIXTURE.read_text())
    for p in data["persons"]:
        await world.person(p["name"], {"user-stephen": "stephen", "user-stephanie": "stephanie"}[p["user_id"]],
                           p["device_trackers"])
    return data


async def test_dry_run_against_todays_server_changes_nothing(hass, world, caplog) -> None:
    """The fixture is the real registries and states; the one registration whose owner can't be read over a
    read-only API (``device_tracker.iphone_17_pro``, "iPhone 13 Pro (6)") is given to nobody in this test."""
    caplog.set_level(logging.INFO)
    data = await _build_real(hass, world, unknown_owner="someone-else", source=SOURCE_IMPORT)
    assert world.inputs("device_tracker.stephen_location") == [
        "device_tracker.stephen_s_iphone_fenton", "device_tracker.iphone_fenton"]
    assert world.inputs("device_tracker.stephanie_location") == ["device_tracker.stephanies_iphone"]
    assert [p["device_trackers"] for p in data["persons"]] == [["device_tracker.stephen_location"],
                                                               ["device_tracker.stephanie_location"]]
    plan = await async_plan(hass, DRY)
    assert plan.actions == [] and plan.composite_writes == [] and plan.person_writes == [] and plan.issues == {}
    assert await presence(hass, **DRY).async_reconcile() == []
    assert "would" not in caplog.text and not ir.async_get(hass).issues


async def test_if_the_old_phone_is_stephens_presence_would_add_it_unless_excluded(hass, world) -> None:
    """``iphone_17_pro`` is Stephen's (checked 2026-11-02) and excluded by default; without that, a composite takes it in."""
    await _build_real(hass, world, unknown_owner="stephen", source=SOURCE_IMPORT)
    plan = await async_plan(hass, {**DRY, CONF_PRESENCE_EXCLUDE: ["device_tracker.iphone_18_pro", "device_tracker.ipad"]})
    assert plan.actions == [] and ISSUE_PRESENCE_COMPOSITE_YAML in plan.issues, "YAML composite: reported, not written"
    assert plan.issues[ISSUE_PRESENCE_COMPOSITE_YAML][1]["trackers"] == "device_tracker.stephen_location"
    plan = await async_plan(hass, DRY)
    assert plan.actions == [] and plan.issues == {}


async def test_ui_composite_on_the_real_state_with_the_old_phone_unowned_is_quiet(hass, world) -> None:
    await _build_real(hass, world, unknown_owner="someone-else", source=SOURCE_USER)
    assert (await async_plan(hass, ON)).actions == []


# -- enrolment ---------------------------------------------------------------------------------------------------------


async def test_a_new_registration_moves_from_the_person_into_the_composite_and_back_out_on_removal(
        hass, world, caplog) -> None:
    caplog.set_level(logging.INFO)
    pid = await house(world)
    p = presence(hass, **ON)
    before = world.inputs("device_tracker.stephen_location")
    # mobile_app registers a new device and adds its tracker straight to the person.
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    assert world.person_trackers(pid) == ["device_tracker.stephen_location", "device_tracker.new_phone"]

    actions = await p.async_reconcile()
    assert [a.kind for a in actions] == ["composite", "person"]
    assert world.inputs("device_tracker.stephen_location") == [*before, "device_tracker.new_phone"]
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
    assert "Presence changed device_tracker.stephen_location for Stephen: add device_tracker.new_phone" in caplog.text
    assert world.inputs("device_tracker.stephanie_location") == ["device_tracker.steph_phone"], "others untouched"

    # The registration goes (1.1: sign-out, sweep or admin); its entities go with it and it drops out.
    er.async_get(hass).async_remove("device_tracker.new_phone")
    hass.states.async_remove("device_tracker.new_phone")
    actions = await p.async_reconcile()
    assert [a.kind for a in actions] == ["composite"]
    assert world.inputs("device_tracker.stephen_location") == before
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]


async def test_an_excluded_tracker_stays_out_and_is_taken_off_the_person(hass, world) -> None:
    pid = await house(world)
    world.tracker("device_tracker.iphone_18_pro", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.iphone_18_pro")
    await presence(hass, **ON).async_reconcile()
    assert "device_tracker.iphone_18_pro" not in world.inputs("device_tracker.stephen_location")
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
    # Take it off the exclude list and it is enrolled; put it back and it is dropped again.
    free = presence(hass, **ON, **{CONF_PRESENCE_EXCLUDE: []})
    await free.async_reconcile()
    assert "device_tracker.iphone_18_pro" in world.inputs("device_tracker.stephen_location")
    await presence(hass, **ON).async_reconcile()
    assert "device_tracker.iphone_18_pro" not in world.inputs("device_tracker.stephen_location")


async def test_inputs_of_other_owners_and_vanished_ones_go_but_other_integrations_stay(hass, world) -> None:
    await house(world)
    world.tracker("device_tracker.car", "nobody", platform="fordpass")
    entry = world.composites["device_tracker.stephen_location"]
    inputs = [*world.inputs("device_tracker.stephen_location"), "device_tracker.steph_phone", "device_tracker.car",
              "device_tracker.long_gone"]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "entity_id": [
        {"entity": i, "all_states": i == "device_tracker.duo", "use_picture": False} for i in inputs]})
    await presence(hass, **ON).async_reconcile()
    assert world.inputs("device_tracker.stephen_location") == ["device_tracker.duo", "device_tracker.phone",
                                                               "device_tracker.car"]
    kept = next(c for c in entry.options["entity_id"] if c["entity"] == "device_tracker.duo")
    assert kept["all_states"] is True, "an input's own settings survive"
    assert entry.options["require_movement"] is True


async def test_a_disabled_registration_tracker_leaves_the_composite(hass, world) -> None:
    await house(world)
    er.async_get(hass).async_update_entity("device_tracker.phone", disabled_by=er.RegistryEntryDisabler.USER)
    await presence(hass, **ON).async_reconcile()
    assert world.inputs("device_tracker.stephen_location") == ["device_tracker.duo"]


async def test_the_last_input_is_never_removed(hass, world, caplog) -> None:
    await house(world)
    for entity_id in ("device_tracker.duo", "device_tracker.phone"):
        er.async_get(hass).async_remove(entity_id)
        hass.states.async_remove(entity_id)
    await presence(hass, **ON).async_reconcile()
    assert world.inputs("device_tracker.stephen_location") == ["device_tracker.duo", "device_tracker.phone"]
    assert "left with no tracker" in caplog.text


# -- missing composite -------------------------------------------------------------------------------------------------


async def test_composite_not_installed_raises_a_repair_and_changes_nothing(hass) -> None:
    assert await async_setup_component(hass, "person", {})
    world = World(hass)
    pid = await house_without_composite(world)
    p = presence(hass, **ON)
    assert await p.async_reconcile() == []
    assert world.person_trackers(pid) == ["device_tracker.duo"], "the person is left exactly as mobile_app made it"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PRESENCE_COMPOSITE_MISSING)


async def house_without_composite(world: World) -> str:
    await world.user("stephen")
    world.tracker("device_tracker.duo", "stephen")
    return await world.person("Stephen", "stephen", ["device_tracker.duo"])


async def test_a_person_without_a_composite_is_left_alone_with_a_repair(hass, world) -> None:
    pid = await house_without_composite(world)
    other = await house_other(world)
    p = presence(hass, **ON)
    assert await p.async_reconcile() == []
    assert world.person_trackers(pid) == ["device_tracker.duo"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PRESENCE_NO_COMPOSITE}_{pid}")
    # The repair clears once the person has a composite.
    world.composite("device_tracker.stephen_location", ["device_tracker.duo"])
    await hass.data["person"][1].async_update_item(pid, {"device_trackers": ["device_tracker.stephen_location",
                                                                           "device_tracker.duo"]})
    await p.async_reconcile()
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PRESENCE_NO_COMPOSITE}_{pid}")
    assert other


async def house_other(world: World) -> str:
    await world.user("sam")
    return await world.person("Sam", "sam", [])      # nothing to track: nothing to complain about


# -- modes -------------------------------------------------------------------------------------------------------------


async def test_dry_run_logs_and_changes_nothing(hass, world, caplog) -> None:
    caplog.set_level(logging.INFO)
    pid = await house(world)
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    entry = world.composites["device_tracker.stephen_location"]
    before = (dict(entry.options), world.person_trackers(pid))
    actions = await presence(hass, **DRY).async_reconcile()
    assert [a.kind for a in actions] == ["composite", "person"]
    assert (dict(entry.options), world.person_trackers(pid)) == before
    assert "Presence (dry run) would change device_tracker.stephen_location for Stephen: add device_tracker.new_phone" \
        in caplog.text and "Presence changed" not in caplog.text


async def test_dry_run_is_the_default_and_off_does_nothing(hass, world) -> None:
    pid = await house(world)
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    assert await presence(hass, **{CONF_PRESENCE_ENABLED: True}).async_reconcile() != []
    assert world.person_trackers(pid) == ["device_tracker.stephen_location", "device_tracker.new_phone"]
    assert await presence(hass).async_reconcile() == []      # presence_enabled defaults to off
    assert world.person_trackers(pid) == ["device_tracker.stephen_location", "device_tracker.new_phone"]
    assert not ir.async_get(hass).issues


async def test_idempotent_no_writes_when_nothing_differs(hass, world) -> None:
    await house(world)
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    p = presence(hass, **ON)
    assert await p.async_reconcile()
    with patch.object(hass.config_entries, "async_update_entry") as update, \
            patch.object(hass.data["person"][1], "async_update_item") as update_person:
        assert await p.async_reconcile() == [] and await p.async_reconcile() == []
    update.assert_not_called()
    update_person.assert_not_called()


async def test_a_yaml_composite_is_reported_not_written(hass, world, caplog) -> None:
    caplog.set_level(logging.INFO)
    pid = await house(world, source=SOURCE_IMPORT)
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    entry = world.composites["device_tracker.stephen_location"]
    before = dict(entry.options)
    assert await presence(hass, **ON).async_reconcile() == []
    assert dict(entry.options) == before
    assert "device_tracker.new_phone" in world.person_trackers(pid), "not taken off the person: nothing covers it"
    assert "managed by YAML" in caplog.text
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PRESENCE_COMPOSITE_YAML)


# -- wiring ------------------------------------------------------------------------------------------------------------


async def test_registry_events_and_person_changes_trigger_a_pass(hass, world) -> None:
    pid = await house(world)
    p = presence(hass, **ON)
    p.start()
    try:
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()
        # A new registration: entity created, then mobile_app adds it to the person.
        world.tracker("device_tracker.new_phone", "stephen")
        await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
        await hass.async_block_till_done()
        assert "device_tracker.new_phone" in world.inputs("device_tracker.stephen_location")
        assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
        # Removal.
        er.async_get(hass).async_remove("device_tracker.new_phone")
        hass.states.async_remove("device_tracker.new_phone")      # the entity takes its state with it
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
        await hass.async_block_till_done()
        assert "device_tracker.new_phone" not in world.inputs("device_tracker.stephen_location")
    finally:
        p.stop()


async def test_unrelated_registry_events_do_not_schedule_a_pass(hass, world) -> None:
    await house(world)
    p = presence(hass, **ON)
    p.start()
    try:
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()
        assert p._pending is None
        er.async_get(hass).async_get_or_create("sensor", "demo", "x", suggested_object_id="x")
        er.async_get(hass).async_update_entity("device_tracker.duo", name="Duo")
        await hass.async_block_till_done()
        assert p._pending is None
    finally:
        p.stop()


async def test_the_entry_runs_presence_and_its_options_are_in_the_flow(hass, world, configured) -> None:
    runtime = hass.data[DOMAIN][configured.entry_id]
    assert runtime.presence is not None
    result = await hass.config_entries.options.async_init(configured.entry_id)
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys[CONF_PRESENCE_ENABLED].default() is False and keys[CONF_PRESENCE_DRY_RUN].default() is True
    assert keys[CONF_PRESENCE_EXCLUDE].default() == DEFAULT_PRESENCE_EXCLUDE
    pid = await house(world)
    world.tracker("device_tracker.new_phone", "stephen")
    await person.async_add_user_device_tracker(hass, world.users["stephen"], "device_tracker.new_phone")
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_PRESENCE_ENABLED: True,
                                                                          CONF_PRESENCE_DRY_RUN: False})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert configured.options[CONF_PRESENCE_ENABLED] is True
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"], "turning it on runs a pass"


async def test_on_the_real_state_a_known_tracker_is_moved_into_the_composite_then_restored(hass, world) -> None:
    """Add ``device_tracker.iphone_17_pro`` to Stephen the way mobile_app would, with the composites in the UI."""
    await _build_real(hass, world, unknown_owner="stephen", source=SOURCE_USER)
    pid = next(p["id"] for p in hass.data["person"][1].async_items() if p["name"] == "Stephen Fenton")
    original = world.inputs("device_tracker.stephen_location")
    await hass.data["person"][1].async_update_item(pid, {"device_trackers": [
        "device_tracker.stephen_location", "device_tracker.iphone_17_pro"]})
    await presence(hass, **ON, **{CONF_PRESENCE_EXCLUDE: ["device_tracker.iphone_18_pro", "device_tracker.ipad"]}).async_reconcile()
    assert world.inputs("device_tracker.stephen_location") == [*original, "device_tracker.iphone_17_pro"]
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
    # Restore: put the phone on the exclude list and the composite is back to what the server has today.
    await presence(hass, **ON).async_reconcile()
    assert world.inputs("device_tracker.stephen_location") == original
    assert world.person_trackers(pid) == ["device_tracker.stephen_location"]
    assert world.inputs("device_tracker.stephanie_location") == ["device_tracker.stephanies_iphone"]


async def test_start_before_running_waits_for_started_and_unloads_cleanly(hass, world, caplog) -> None:
    from homeassistant.core import CoreState  # noqa: PLC0415

    await house(world)
    hass.set_state(CoreState.starting)
    p = presence(hass, **ON)
    p.start()
    hass.set_state(CoreState.running)
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    p.stop()      # the listen_once already fired: removing it again would log an error
    assert "Unable to remove unknown job listener" not in caplog.text
