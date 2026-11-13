"""The job engine inside Home Assistant: states in, notify calls out, buttons, dismissals and problems.

``mobile_app`` can't load here (it pulls in cloud and assist), so ``MobileAppStandIn`` records what the engine sends
through ``notify.mobile_app_<device>``. Donetick and the todo list are stand-in actions with the same shapes.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fenton_home import entities, recovery
from custom_components.fenton_home.const import (
    CONF_LIVE_DEVICES,
    CONF_LIVE_JOBS,
    CONF_LIVE_JOBS_ENABLED,
    DOMAIN,
    SENSOR_JOBS,
)
from custom_components.fenton_home.live_jobs import LiveJobs

from .test_integration import (
    DATA,
    PUSH_URL,
    STEPHANIE,
    STEPHEN,
    mobile_app_actions,
    mobile_app_entry,
)

TODO = "todo.all_tasks_internal"
TOKEN = "ab" * 32
LIVE_DATA = {"push_url": PUSH_URL, "push_token": "tok", "apns_environment": "sandbox", "start_live_activity_token": TOKEN}


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


class Donetick:
    """``todo.get_items`` and ``donetick.complete_task`` the way the real ones answer."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.tasks: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.fail = False
        hass.states.async_set(TODO, "0")
        hass.services.async_register("todo", "get_items", self._get_items, supports_response=SupportsResponse.ONLY)
        hass.services.async_register("donetick", "complete_task", self._complete)

    def add(self, summary: str, task_id: int) -> None:
        self.tasks.append({"summary": summary, "uid": f"{task_id}--abc", "status": "needs_action"})
        self.hass.states.async_set(TODO, str(len(self.tasks)))

    def finish(self, task_id: int) -> None:
        self.tasks = [t for t in self.tasks if not t["uid"].startswith(f"{task_id}--")]
        self.hass.states.async_set(TODO, str(len(self.tasks)))

    async def _get_items(self, call: ServiceCall) -> dict[str, Any]:
        assert call.data == {"entity_id": TODO, "status": "needs_action"}
        return {TODO: {"items": list(self.tasks)}}

    async def _complete(self, call: ServiceCall) -> None:
        self.completed.append(dict(call.data) | {"user_id": call.context.user_id})
        if self.fail:
            raise HomeAssistantError("Donetick is down")
        self.finish(call.data["task_id"])


class Phones:
    """What reached each person's ordinary notify action."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.sent: list[dict[str, Any]] = []
        hass.services.async_register("notify", "stephanie_s_phone", self._send)
        hass.services.async_register("notify", "stephen_s_phone", self._send_stephen)
        self.stephen: list[dict[str, Any]] = []

    async def _send(self, call: ServiceCall) -> None:
        self.sent.append(dict(call.data))

    async def _send_stephen(self, call: ServiceCall) -> None:
        self.stephen.append(dict(call.data))


@pytest.fixture(autouse=True)
def no_startup_hold(monkeypatch):
    """The startup hold keeps cards while other integrations' entities load; most tests want it off."""
    monkeypatch.setattr("custom_components.fenton_home.live_jobs.STARTUP_HOLD_SECONDS", 0)


@pytest.fixture
async def world(hass: HomeAssistant):
    """Stephen has S+H on an iPhone and an iPad (push-to-start tokens); Stephanie is on another app."""
    assert await async_setup_component(hass, "http", {})
    hass.states.async_set("person.stephen_fenton", "home", {"user_id": STEPHEN, "friendly_name": "Stephen Fenton"})
    hass.states.async_set("person.stephanie_hobart", "home", {"user_id": STEPHANIE, "friendly_name": "Stephanie Hobart"})
    mobile_app_entry(hass, "hook-stephen-iphone", STEPHEN, app_data=dict(LIVE_DATA))
    mobile_app_entry(hass, "hook-stephen-ipad", STEPHEN, model="iPad16,5", app_data=dict(LIVE_DATA))
    mobile_app_entry(hass, "hook-stephanie-iphone", STEPHANIE, app_data={"push_url": PUSH_URL, "push_token": "tok"})
    service = await mobile_app_actions(hass)
    phones = Phones(hass)
    donetick = Donetick(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    live: LiveJobs = hass.data[DOMAIN][entry.entry_id].live
    live.confirm_timeout = 0.3
    live.dismiss_delay = 0
    live.engine.coalescer.window = 0
    return type("World", (), {"hass": hass, "live": live, "service": service, "phones": phones,
                              "donetick": donetick, "entry": entry})


def sent(world, webhook: str | None = None, since: int = 0) -> list[dict[str, Any]]:
    return [s for s in world.service.sent[since:] if webhook is None or s["target"] == webhook]


def start_fan(hass: HomeAssistant, room: str = "guest", ends_in: float = 1500) -> None:
    hass.states.async_set(f"input_datetime.{room}_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + ends_in})
    hass.states.async_set(f"input_boolean.{room}_bathroom_fan_timer_pending", "on")


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()


def register_script(hass: HomeAssistant, room: str = "guest", *, works: bool = True, raises: bool = False) -> list[dict]:
    calls: list[dict] = []

    async def command(call: ServiceCall) -> None:
        calls.append(dict(call.data) | {"user_id": call.context.user_id})
        if raises:
            raise HomeAssistantError("script failed")
        if works:
            hass.states.async_set(f"input_boolean.{room}_bathroom_fan_timer_pending", "off")

    hass.services.async_register("script", f"{room}_bathroom_fan_command", command)
    return calls


# -- Cards ------------------------------------------------------------------------------------------------------------


async def test_a_fan_timer_starts_a_card_on_every_registration_with_a_start_token(hass: HomeAssistant, world) -> None:
    start_fan(hass)
    await settle(hass)
    assert sorted(s["target"] for s in sent(world)) == ["hook-stephen-ipad", "hook-stephen-iphone"]
    message = sent(world, "hook-stephen-iphone")[0]
    data = message["data"]
    assert message["title"] == "Guest Bath Fan" and data["live_update"] is True and data["tag"] == "job.fan.guest"
    assert data["fenton_attributes"] == {"kind": "fan", "symbol": "fan", "tint": "air"}
    activity = data["fenton_activity"]
    assert activity["v"] == 2 and activity["phase"] == "running" and activity["value"]["kind"] == "countdown"
    assert activity["controls"] == [{"id": "stop", "title": "Stop"}] and "live_alert" not in data
    assert 65 < data["relevance_score"] < 70
    # Stephanie's app has no push-to-start token: she gets no card.
    assert "hook-stephanie-iphone" not in {s["target"] for s in sent(world)}
    state = hass.states.get(SENSOR_JOBS)
    assert state.state == "2" and state.attributes["tags"]["job.fan.guest"]["phase"] == "running"


async def test_the_card_ends_with_clear_notification_when_the_job_does(hass: HomeAssistant, world) -> None:
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    hass.states.async_set("input_boolean.guest_bathroom_fan_timer_pending", "off")
    await settle(hass)
    ends = sent(world, since=count)
    assert [(s["target"], s["message"], s["data"]) for s in ends] == [
        ("hook-stephen-iphone", "clear_notification", {"tag": "job.fan.guest"}),
        ("hook-stephen-ipad", "clear_notification", {"tag": "job.fan.guest"})] or len(ends) == 2
    assert hass.states.get(SENSOR_JOBS).state == "0"


async def test_options_choose_jobs_and_devices(hass: HomeAssistant, world) -> None:
    hass.config_entries.async_update_entry(world.entry, options={CONF_LIVE_JOBS: ["dishwasher"]})
    await settle(hass)
    start_fan(hass)
    await settle(hass)
    assert world.service.sent == [], "the fan job is off"
    hass.config_entries.async_update_entry(world.entry, options={CONF_LIVE_JOBS: ["fan.guest", "fan.master"],
                                                                 CONF_LIVE_DEVICES: ["hook-stephen-iphone"]})
    await settle(hass)
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone"}
    count = len(world.service.sent)
    hass.config_entries.async_update_entry(world.entry, options={CONF_LIVE_JOBS_ENABLED: False})
    await settle(hass)
    assert [s["message"] for s in sent(world, since=count)] == ["clear_notification"], "switching off ends what showed"


async def test_a_new_registration_with_a_token_gets_the_running_jobs(hass: HomeAssistant, world) -> None:
    mobile_app_entry(hass, "hook-stephanie-s-plus-h", STEPHANIE, app_data=dict(LIVE_DATA))
    await world.service.async_register_services()
    start_fan(hass)
    await settle(hass)
    assert "hook-stephanie-s-plus-h" in {s["target"] for s in sent(world)}


async def test_a_missing_notify_action_is_logged_and_retried(hass: HomeAssistant, world, caplog) -> None:
    mobile_app_entry(hass, "hook-nameless", STEPHEN, app_data=dict(LIVE_DATA))
    world.service.unregistered.add("hook-nameless")  # mobile_app has no entry for it
    start_fan(hass)
    await settle(hass)
    assert "No notify action for hook-nameless · Fenton" in caplog.text
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone", "hook-stephen-ipad"}


async def test_a_send_that_raises_is_logged_and_sent_again_on_the_next_change(hass: HomeAssistant, world, caplog) -> None:
    async def broken(message: str = "", **kwargs: Any) -> None:
        raise HomeAssistantError("push down")

    original = world.service.async_send_message
    world.service.async_send_message = broken
    start_fan(hass)
    await settle(hass)
    assert "Couldn't send job.fan.guest" in caplog.text
    world.service.async_send_message = original
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 1700})
    await settle(hass)
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone", "hook-stephen-ipad"}


# -- A registration that lost its push-to-start token -------------------------------------------------------------------


def mobile_entry(hass: HomeAssistant, webhook_id: str):
    return next(e for e in hass.config_entries.async_entries("mobile_app") if e.data["webhook_id"] == webhook_id)


def set_app_data(hass: HomeAssistant, webhook_id: str, app_data: dict[str, Any]) -> None:
    entry = mobile_entry(hass, webhook_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "app_data": app_data})


def drop_start_token(hass: HomeAssistant, webhook_id: str) -> None:
    """The app's ``update_registration`` replaced ``app_data`` without ``start_live_activity_token``."""
    set_app_data(hass, webhook_id, {k: v for k, v in mobile_entry(hass, webhook_id).data["app_data"].items()
                                    if k != "start_live_activity_token"})


async def test_a_registration_that_loses_its_start_token_still_gets_the_update_and_the_end(hass: HomeAssistant,
                                                                                            world) -> None:
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    drop_start_token(hass, "hook-stephen-ipad")
    await settle(hass)
    assert sent(world, since=count) == [], "losing the token ends nothing"
    assert ("hook-stephen-ipad", "job.fan.guest") in world.live.engine.cards
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 1800})
    await settle(hass)
    assert {s["target"] for s in sent(world, since=count)} == {"hook-stephen-iphone", "hook-stephen-ipad"}
    count = len(world.service.sent)
    hass.states.async_set("input_boolean.guest_bathroom_fan_timer_pending", "off")
    await settle(hass)
    ends = sent(world, since=count)
    assert {(s["target"], s["message"]) for s in ends} == {("hook-stephen-iphone", "clear_notification"),
                                                           ("hook-stephen-ipad", "clear_notification")}
    assert world.live.engine.cards == {}


async def test_a_registration_without_a_start_token_is_not_given_a_new_card(hass: HomeAssistant, world) -> None:
    drop_start_token(hass, "hook-stephen-ipad")
    start_fan(hass)
    await settle(hass)
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone"}
    set_app_data(hass, "hook-stephen-ipad", dict(LIVE_DATA))
    await settle(hass)
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone", "hook-stephen-ipad"}


async def test_a_card_ends_as_not_eligible_only_when_the_registration_cannot_push_or_is_gone(hass: HomeAssistant,
                                                                                              world, caplog) -> None:
    start_fan(hass)
    await settle(hass)
    set_app_data(hass, "hook-stephen-ipad", {})  # signed out: nothing can reach it any more
    await settle(hass)
    assert "card end tag=job.fan.guest reason=not eligible" in caplog.text
    assert ("hook-stephen-ipad", "job.fan.guest") not in world.live.engine.cards


# -- A manual vacuum run whose status flag follows the vacuum ----------------------------------------------------------

VACUUM_TAG = "job.vacuum.music_room"


def card_pushes(world, webhook: str = "hook-stephen-iphone") -> list[dict[str, Any]]:
    return [s for s in sent(world, webhook) if s["data"]["tag"] == VACUUM_TAG and s["message"] != "clear_notification"]


async def test_a_vacuum_whose_flag_follows_it_by_a_moment_is_one_card_not_a_start_an_end_and_a_start(
        hass: HomeAssistant, world) -> None:
    """2026-11-13 03:00:00: the vacuum read ``cleaning`` and 8 ms later its status flag read ``segment``."""
    segment = "sensor.music_room_music_room_robot_vacuum_estimated_segment"
    hass.states.async_set(segment, "Music Room")
    hass.states.async_set("sensor.valetudo_elatedusedram_status_flag", "none")
    hass.states.async_set("vacuum.valetudo_elatedusedram", "cleaning")
    await settle(hass)
    card = world.live.engine.cards[("hook-stephen-iphone", VACUUM_TAG)]
    assert card_pushes(world)[-1]["data"]["fenton_activity"]["headline"] == "Cleaning Music Room"
    hass.states.async_set("sensor.valetudo_elatedusedram_status_flag", "segment")
    await settle(hass)
    assert card_pushes(world)[-1]["data"]["fenton_activity"]["headline"] == "Vacuuming Music Room"
    # The robot crosses a doorway for a few seconds and comes back: still the one card.
    hass.states.async_set(segment, "Pantry")
    await settle(hass)
    hass.states.async_set(segment, "Music Room")
    await settle(hass)
    assert [s for s in sent(world) if s["message"] == "clear_notification"] == []
    assert world.live.engine.cards[("hook-stephen-iphone", VACUUM_TAG)] is card and card.session_key == "none:"


# -- Buttons ----------------------------------------------------------------------------------------------------------


async def press(hass: HomeAssistant, tag: str, control: str, user_id: str | None = STEPHEN) -> None:
    hass.bus.async_fire("fenton_live_activity_action", {"tag": tag, "job": tag.removeprefix("job."), "control": control},
                        context=Context(user_id=user_id))
    await hass.async_block_till_done()


async def test_stop_runs_the_script_waits_for_the_state_and_the_card_goes(hass: HomeAssistant, world) -> None:
    calls = register_script(hass)
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.fan.guest", "stop")
    assert calls == [{"command": "power", "target_power": "off", "user_id": STEPHEN}]
    assert [s["message"] for s in sent(world, since=count)] == ["clear_notification", "clear_notification"]


async def test_a_press_from_phones_that_lost_their_start_token_works_for_the_card_they_show(hass: HomeAssistant,
                                                                                            world, caplog) -> None:
    calls = register_script(hass)
    start_fan(hass)
    await settle(hass)
    drop_start_token(hass, "hook-stephen-iphone")
    drop_start_token(hass, "hook-stephen-ipad")
    await settle(hass)
    await press(hass, "job.fan.guest", "stop")
    assert calls == [{"command": "power", "target_power": "off", "user_id": STEPHEN}]
    # A tag neither registration shows is refused: neither can start one, so neither is a card device for it.
    await press(hass, "job.fan.master", "stop")
    assert len(calls) == 1 and "has no card-capable registration" in caplog.text


async def test_a_stop_that_never_takes_effect_pushes_the_real_state_with_a_notice(hass: HomeAssistant, world) -> None:
    calls = register_script(hass, works=False)
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.fan.guest", "stop")
    assert len(calls) == 1
    pushes = sent(world, "hook-stephen-iphone", since=count)
    assert len(pushes) == 1
    data = pushes[0]["data"]
    assert data["fenton_activity"]["notice"] == "Couldn't stop. Try again." and data["urgent"] is True
    assert data["fenton_activity"]["controls"] == [{"id": "stop", "title": "Stop"}], "the controls are back"
    # The notice is for the device-owner who pressed; once.
    count = len(world.service.sent)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 1700})
    await settle(hass)
    assert all("notice" not in s["data"]["fenton_activity"] for s in sent(world, since=count))


async def test_each_press_and_each_card_start_update_and_end_is_logged_once(hass: HomeAssistant, world, caplog) -> None:
    caplog.set_level("INFO", logger="custom_components.fenton_home.live_jobs")
    register_script(hass)
    start_fan(hass)
    await settle(hass)
    await press(hass, "job.fan.guest", "stop")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fenton_home.")]
    assert "fenton_home.card start tag=job.fan.guest phase=running alert=False" in lines
    assert f"fenton_home.press tag=job.fan.guest control=stop user={STEPHEN} outcome=confirmed" in lines
    assert any(line.startswith("fenton_home.card end tag=job.fan.guest reason=") for line in lines)
    assert "hook-" not in caplog.text, "no webhook id (a secret) in a log line"


async def test_a_press_that_times_out_logs_the_outcome(hass: HomeAssistant, world, caplog) -> None:
    caplog.set_level("INFO", logger="custom_components.fenton_home.live_jobs")
    register_script(hass, works=False)
    start_fan(hass)
    await settle(hass)
    await press(hass, "job.fan.guest", "stop")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fenton_home.press")]
    assert lines == [f"fenton_home.press tag=job.fan.guest control=stop user={STEPHEN} outcome=timed out"]


async def test_a_press_that_raises_or_is_refused_logs_the_outcome(hass: HomeAssistant, world, caplog) -> None:
    caplog.set_level("INFO", logger="custom_components.fenton_home.live_jobs")
    register_script(hass, raises=True)
    start_fan(hass)
    await settle(hass)
    await press(hass, "job.fan.guest", "stop")
    await press(hass, "job.fan.guest", "self_destruct")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fenton_home.press")]
    assert lines == [f"fenton_home.press tag=job.fan.guest control=stop user={STEPHEN} outcome=service error",
                     f"fenton_home.press tag=job.fan.guest control=self_destruct user={STEPHEN} outcome=refused"]


async def test_a_service_error_is_reported_the_same_way(hass: HomeAssistant, world) -> None:
    register_script(hass, raises=True)
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.fan.guest", "stop")
    assert sent(world, "hook-stephen-iphone", since=count)[0]["data"]["fenton_activity"]["notice"] == \
        "Couldn't stop. Try again."


async def test_a_press_with_no_user_or_an_unknown_user_does_nothing(hass: HomeAssistant, world, caplog) -> None:
    calls = register_script(hass)
    start_fan(hass)
    await settle(hass)
    await press(hass, "job.fan.guest", "stop", user_id=None)
    await press(hass, "job.fan.guest", "stop", user_id="user-stranger")
    await press(hass, "job.fan.guest", "stop", user_id=STEPHANIE)  # her app can't show cards
    assert calls == []
    assert "has no card-capable registration" in caplog.text and "has no user" in caplog.text


async def test_a_control_the_card_does_not_offer_is_refused_with_a_notice(hass: HomeAssistant, world) -> None:
    calls = register_script(hass)
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.fan.guest", "self_destruct")
    await press(hass, "job.washer", "stop")  # not even running
    assert calls == []
    assert sent(world, "hook-stephen-iphone", since=count)[0]["data"]["fenton_activity"]["notice"] == "No longer available"


async def test_pause_and_resume_update_the_card(hass: HomeAssistant, world) -> None:
    started = time.time() - 60
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "cleaning")
    hass.states.async_set("binary_sensor.main_floor_vacuum_coordinator_auto_cleaning", "on")
    hass.states.async_set("sensor.main_floor_vacuum_coordinator_kitchen_last_cleaned", "x", {"room_name": "Kitchen"})
    hass.states.async_set("sensor.main_floor_vacuum_coordinator_current_room", "kitchen", {
        "room_id": "kitchen", "phase": "cleaning", "start_confirmed_at": iso(started)})
    await settle(hass)
    assert sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]["headline"] == "Cleaning Kitchen"
    paused: list[dict] = []

    async def pause(call: ServiceCall) -> None:
        paused.append(dict(call.data))
        hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "paused")

    hass.services.async_register("vacuum", "pause", pause)
    count = len(world.service.sent)
    await press(hass, "job.vacuum.main_floor", "pause")
    assert paused == [{"entity_id": "vacuum.valetudo_exaltedsneakydeer"}]
    # The state change and the confirmation collapse to one push per device, at priority 10, with the new phase.
    mine = sent(world, "hook-stephen-iphone", since=count)
    assert mine and mine[-1]["data"]["fenton_activity"]["phase"] == "paused" and mine[-1]["data"]["urgent"] is True
    assert mine[-1]["data"]["fenton_activity"]["controls"][0]["id"] == "resume"


# -- Dismissals -------------------------------------------------------------------------------------------------------


async def test_a_dismissal_from_a_user_with_one_device_showing_the_card(hass: HomeAssistant, world) -> None:
    mobile_app_entry(hass, "hook-stephanie-s-plus-h", STEPHANIE, app_data=dict(LIVE_DATA))
    await world.service.async_register_services()
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": "job.fan.guest"}, context=Context(user_id=STEPHANIE))
    await settle(hass)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 3000})
    await settle(hass)
    assert {s["target"] for s in sent(world, since=count)} == {"hook-stephen-iphone", "hook-stephen-ipad"}


async def test_a_dismissal_event_cannot_name_a_device(hass: HomeAssistant, world) -> None:
    """A webhook id is a credential and events are recorded: the event carries none and one that does is not honoured."""
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": "job.fan.guest", "webhook_id": "hook-stephen-iphone"},
                        context=Context(user_id=STEPHEN))
    await settle(hass)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 3000})
    await settle(hass)
    # The named device is ignored; with no way to tell which device it was, both of Stephen's stay dismissed.
    assert sent(world, since=count) == []


async def test_a_dismissal_without_a_device_goes_to_the_one_whose_token_core_dropped(hass: HomeAssistant, world,
                                                                                      monkeypatch) -> None:
    start_fan(hass)
    await settle(hass)
    monkeypatch.setattr(recovery, "has_token", lambda hass_, webhook, tag: webhook != "hook-stephen-ipad")
    count = len(world.service.sent)
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": "job.fan.guest"}, context=Context(user_id=STEPHEN))
    await settle(hass)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 3000})
    await settle(hass)
    assert {s["target"] for s in sent(world, since=count)} == {"hook-stephen-iphone"}


async def test_dismissals_from_nobody_or_for_nothing_are_ignored(hass: HomeAssistant, world) -> None:
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": "job.fan.guest"})
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": ""}, context=Context(user_id=STEPHEN))
    hass.bus.async_fire("fenton_live_activity_dismissed", {"tag": "job.washer"}, context=Context(user_id=STEPHEN))
    await settle(hass)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 3000})
    await settle(hass)
    assert len(sent(world, since=count)) == 2


# -- Problems ---------------------------------------------------------------------------------------------------------


def stuck(hass: HomeAssistant) -> None:
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "error")
    hass.states.async_set("sensor.main_floor_vacuum_coordinator_current_room", "kitchen", {
        "room_id": "kitchen", "phase": "cleaning"})
    hass.states.async_set("sensor.main_floor_vacuum_coordinator_kitchen_last_cleaned", "x", {"room_name": "Kitchen"})
    hass.states.async_set("sensor.main_floor_vacuum_status", "error", {"current_issue": {
        "code": "navigation.stuck", "raw": "Robot stuck", "reported_at": iso(time.time() - 30)}})


PROBLEM = {"device": "main_floor", "code": "navigation.stuck", "title": "Main Floor Vacuum · Robot Stuck",
           "message": "The vacuum is stuck in the Kitchen.", "task_title": "Main Floor Vacuum · Robot Stuck",
           "url": "/sfenton-react-dash/home?path=vacuums", "tag": "vacuum-kitchen-error"}


async def test_a_problem_is_a_card_for_people_with_s_plus_h_and_a_push_for_everyone_else(hass: HomeAssistant, world) -> None:
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    cards = sent(world, "hook-stephen-iphone")
    assert cards[0]["data"]["live_alert"] is True, "the card turned red once, with sound"
    assert all("live_alert" not in c["data"] for c in cards[1:])
    card = cards[-1]["data"]
    assert card["tag"] == "job.vacuum.main_floor"
    activity = card["fenton_activity"]
    assert activity["phase"] == "problem" and activity["problem"]["task_id"] == 4217
    assert [c["id"] for c in activity["controls"]] == ["resume", "dock", "done"]
    assert activity["problem"]["problem"] == "Stuck in Kitchen" and card["relevance_score"] == 100
    # Stephen's phones (Companion included) get no push: the card replaces it. Stephanie has no S+H and does.
    assert world.phones.stephen == []
    (push,) = world.phones.sent
    assert push == {"message": "The vacuum is stuck in the Kitchen.", "title": "Main Floor Vacuum · Robot Stuck",
                    "data": {"tag": "vacuum-kitchen-error", "url": "/sfenton-react-dash/home?path=vacuums",
                             "clickAction": "/sfenton-react-dash/home?path=vacuums"}}


async def test_done_completes_the_task_ends_the_card_everywhere_and_clears_the_fallback_push(hass: HomeAssistant,
                                                                                            world, monkeypatch) -> None:
    monkeypatch.setitem(entities.DONETICK_USERS, STEPHEN, 1)
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.vacuum.main_floor", "done")
    assert world.donetick.completed == [{"task_id": 4217, "completed_by": 1, "user_id": STEPHEN}]
    ends = sent(world, since=count)
    assert [s["message"] for s in ends] == ["clear_notification", "clear_notification"]
    assert world.phones.sent[-1] == {"message": "clear_notification", "data": {"tag": "vacuum-kitchen-error"}}
    assert hass.states.get(SENSOR_JOBS).state == "0"


async def test_a_task_completed_elsewhere_ends_the_card_while_a_docked_robot_keeps_it_until_then(hass: HomeAssistant,
                                                                                                world) -> None:
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    # The robot recovers and docks with no job running; the task is still open, so the problem card stays.
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "docked")
    hass.states.async_set("sensor.main_floor_vacuum_status", "ok", {"current_issue": {"status": "clear"}})
    await settle(hass)
    assert sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]["phase"] == "problem"
    count = len(world.service.sent)
    world.donetick.finish(4217)  # Home Assistant auto-completes it, or a person does in Donetick
    await settle(hass)
    assert world.phones.sent[-1]["message"] == "clear_notification"
    assert [s["message"] for s in sent(world, since=count)] == ["clear_notification", "clear_notification"]


async def test_a_recovered_robot_that_cleans_again_ends_the_problem_and_shows_the_job(hass: HomeAssistant, world) -> None:
    """Fails without the early-end rule (the card stayed a problem card for good) and without the history check."""
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    clear = {"current_issue": {"status": "clear"}}
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "docked")
    hass.states.async_set("sensor.main_floor_vacuum_status", "ok", clear)
    await settle(hass)
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "cleaning")
    await settle(hass)
    assert sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]["phase"] == "problem", "one reading is not enough"
    world.live.now = lambda: time.time() + 40  # it has kept cleaning with no issue
    world.live.request()
    await settle(hass)
    last = sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]
    assert last["phase"] == "running", "the job card is back while the task is still open"
    assert world.phones.sent[-1]["message"] == "clear_notification"
    assert world.donetick.completed == []


async def test_done_continues_when_donetick_errors_and_reports_when_it_never_completes(hass: HomeAssistant, world) -> None:
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    world.donetick.fail = True
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    count = len(world.service.sent)
    await press(hass, "job.vacuum.main_floor", "done")
    notice = sent(world, "hook-stephen-iphone", since=count)[-1]["data"]["fenton_activity"]["notice"]
    assert notice == "Couldn't complete. Try again."
    # It was already completed by someone: the call errors, the list says done, and that is success.
    world.donetick.finish(4217)
    count = len(world.service.sent)
    await settle(hass)
    assert world.phones.sent[-1]["message"] == "clear_notification"


async def test_a_problem_without_a_task_has_no_done_and_ends_when_the_condition_clears(hass: HomeAssistant, world) -> None:
    hass.states.async_set("vacuum.valetudo_exaltedsneakydeer", "returning")
    hass.states.async_set("sensor.main_floor_vacuum_coordinator_current_room", "none", {})
    hass.states.async_set("sensor.main_floor_vacuum_status", "low", {"current_issue": {
        "code": "power.low_battery", "raw": "Low battery", "reported_at": iso(time.time())}})
    await hass.services.async_call("fenton_home", "problem", {
        "device": "main_floor", "code": "power.low_battery", "title": "Main Floor Vacuum · Low battery",
        "message": "Heading home.", "tag": "vacuum-kitchen-error"}, blocking=True)
    await settle(hass)
    activity = sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]
    assert [c["id"] for c in activity["controls"]] == [] and activity["problem"]["severity"] == "warning"
    assert "task_id" not in activity["problem"] and "live_alert" not in sent(world, "hook-stephen-iphone")[-1]["data"]
    count = len(world.service.sent)
    hass.states.async_set("sensor.main_floor_vacuum_status", "ok", {"current_issue": {"status": "clear"}})
    await settle(hass)
    assert [s["message"] for s in sent(world, since=count)] == ["clear_notification", "clear_notification"]
    assert world.phones.sent[-1] == {"message": "clear_notification", "data": {"tag": "vacuum-kitchen-error"}}


async def test_the_task_is_found_after_the_automation_creates_it(hass: HomeAssistant, world) -> None:
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    assert "task_id" not in sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]["problem"]
    world.live.engine.records["vacuum.main_floor"].last_lookup = 0
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 99)
    await settle(hass)
    problem = sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]["problem"]
    assert problem["task_id"] == 99


async def test_the_dishwasher_rinse_aid_task_goes_through_the_same_service(hass: HomeAssistant, world) -> None:
    world.donetick.add("Dishwasher · Refill rinse aid", 7)
    await hass.services.async_call("fenton_home", "problem", {
        "device": "dishwasher", "code": "dishwasher.rinse_aid", "title": "Dishwasher · Rinse aid",
        "message": "Refill the rinse aid.", "task_title": "Dishwasher · Refill rinse aid", "tag": "dishwasher-rinse"},
        blocking=True)
    await settle(hass)
    activity = sent(world, "hook-stephen-iphone")[-1]["data"]["fenton_activity"]
    assert activity["problem"]["problem"] == "Refill rinse aid" and [c["id"] for c in activity["controls"]] == ["done"]


async def test_the_problem_service_validates_its_input(hass: HomeAssistant, world) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("fenton_home", "problem", {"device": "garage", "code": "x"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("fenton_home", "problem", {"device": "main_floor"}, blocking=True)
    # The robot's raw text is enough when there is no code.
    await hass.services.async_call("fenton_home", "problem", {"device": "music_room", "raw": "Unknown error 12"},
                                   blocking=True)
    await settle(hass)
    assert world.live.engine.records["vacuum.music_room"].code == "vendor.unknown_error_12"
    assert world.phones.sent[-1]["title"] == "Music Room Vacuum: Stopped with an error"


async def test_with_cards_switched_off_everyone_gets_the_push(hass: HomeAssistant, world) -> None:
    hass.config_entries.async_update_entry(world.entry, options={CONF_LIVE_JOBS_ENABLED: False})
    await settle(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    await settle(hass)
    assert len(world.phones.stephen) == 1 and len(world.phones.sent) == 1
    assert world.service.sent == []


async def test_a_missing_person_notify_action_is_logged(hass: HomeAssistant, world, caplog) -> None:
    hass.services.async_remove("notify", "stephanie_s_phone")
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    assert "No notify.stephanie_s_phone for Stephanie Hobart" in caplog.text


async def test_the_person_notify_action_name_is_an_option(hass: HomeAssistant, world) -> None:
    async def custom(call: ServiceCall) -> None:
        world.phones.sent.append({"custom": True})

    hass.services.async_register("notify", "phone_of_stephanie", custom)
    hass.config_entries.async_update_entry(world.entry, options={"person_notify": "phone_of_*"})
    await settle(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    assert world.phones.sent == [{"custom": True}]


# -- Lifecycle --------------------------------------------------------------------------------------------------------


async def test_cards_and_problems_survive_a_restart(hass: HomeAssistant, world, hass_storage) -> None:
    world.donetick.add("Main Floor Vacuum · Robot Stuck", 4217)
    stuck(hass)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    start_fan(hass)
    await settle(hass)
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    saved = hass_storage["fenton_home.live_jobs"]["data"]
    assert {(c["webhook_id"], c["tag"]) for c in saved["cards"]} >= {("hook-stephen-iphone", "job.fan.guest")}
    assert saved["records"][0]["task_id"] == 4217
    # While Home Assistant is down the fan stops. On the way back up the engine ends the orphaned card.
    hass.states.async_set("input_boolean.guest_bathroom_fan_timer_pending", "off")
    count = len(world.service.sent)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await settle(hass)
    ended = [(s["target"], s["data"]["tag"]) for s in sent(world, since=count) if s["message"] == "clear_notification"]
    assert ("hook-stephen-iphone", "job.fan.guest") in ended


async def test_unload_removes_the_service_and_the_listeners(hass: HomeAssistant, world) -> None:
    assert hass.services.has_service("fenton_home", "problem")
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    assert not hass.services.has_service("fenton_home", "problem")
    count = len(world.service.sent)
    start_fan(hass)
    await settle(hass)
    assert len(world.service.sent) == count


async def test_a_push_held_back_by_coalescing_goes_when_the_window_passes(hass: HomeAssistant, world) -> None:
    world.live.engine.coalescer.window = 2.0
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 2400})
    await settle(hass)
    assert len(world.service.sent) == count, "one push per tag per two seconds"
    world.live.now = lambda: time.time() + 3  # the clock moves on...
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))  # ...and the engine's wake-up fires
    await settle(hass)
    pushes = sent(world, since=count)
    assert len(pushes) == 2 and pushes[0]["data"]["fenton_activity"]["value"]["end_ts"] >= time.time() + 2300


async def test_one_bad_cycle_is_logged_and_the_next_one_runs(hass: HomeAssistant, world, caplog, monkeypatch) -> None:
    calls = {"n": 0}
    real = world.live.engine.refresh

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(world.live.engine, "refresh", flaky)
    start_fan(hass)
    await settle(hass)
    assert "Live job cycle failed" in caplog.text
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 2400})
    await settle(hass)
    assert sent(world), "the engine carried on"


async def test_garbage_in_the_store_is_skipped(hass: HomeAssistant, world, hass_storage) -> None:
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    hass_storage["fenton_home.live_jobs"] = {"version": 1, "key": "fenton_home.live_jobs", "data": {
        "cards": [{"webhook_id": "x"}, {"webhook_id": "h", "tag": "t", "started_at": "soon"}],
        "records": [{"job_id": "dishwasher", "bogus": 1}, {"nope": True}]}}
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await settle(hass)
    live = hass.data[DOMAIN][world.entry.entry_id].live
    assert live.engine.cards == {} and live.engine.records == {}


async def test_a_person_notify_that_raises_is_logged(hass: HomeAssistant, world, caplog) -> None:
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("phone off")

    hass.services.async_register("notify", "stephanie_s_phone", broken)
    await hass.services.async_call("fenton_home", "problem", PROBLEM, blocking=True)
    assert "Couldn't notify Stephanie Hobart: phone off" in caplog.text
    assert world.live.engine.records["vacuum.main_floor"].pushed_to == []


async def test_after_a_restart_cards_are_kept_while_other_integrations_load(hass: HomeAssistant, world, hass_storage,
                                                                           monkeypatch) -> None:
    start_fan(hass)
    await settle(hass)
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    hass.states.async_remove("input_boolean.guest_bathroom_fan_timer_pending")  # not loaded yet, as at startup
    monkeypatch.setattr("custom_components.fenton_home.live_jobs.STARTUP_HOLD_SECONDS", 120)
    count = len(world.service.sent)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await settle(hass)
    assert [s for s in sent(world, since=count) if s["message"] == "clear_notification"] == []
    live = hass.data[DOMAIN][world.entry.entry_id].live
    assert ("hook-stephen-iphone", "job.fan.guest") in live.engine.cards
    live.now = lambda: time.time() + 200  # the hold is over and the fan really is gone
    live.request()
    await settle(hass)
    assert [s["message"] for s in sent(world, since=count)].count("clear_notification") == 2


async def test_nothing_runs_before_home_assistant_has_started(hass: HomeAssistant, world) -> None:
    from homeassistant.core import CoreState  # noqa: PLC0415

    assert await hass.config_entries.async_unload(world.entry.entry_id)
    hass.set_state(CoreState.starting)
    start_fan(hass)
    count = len(world.service.sent)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await settle(hass)
    assert len(world.service.sent) == count
    hass.set_state(CoreState.running)
    hass.bus.async_fire("homeassistant_started")
    await settle(hass)
    assert {s["target"] for s in sent(world, since=count)} == {"hook-stephen-iphone", "hook-stephen-ipad"}


async def test_devices_sharing_a_name_each_get_their_card(hass: HomeAssistant, world) -> None:
    for webhook in ("hook-stephen-ipad", "hook-stephen-iphone"):
        entry = next(e for e in hass.config_entries.async_entries("mobile_app") if e.data["webhook_id"] == webhook)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "device_name": "Stephen's iPhone · Fenton"})
    start_fan(hass)
    await settle(hass)
    assert sorted(s["target"] for s in sent(world)) == ["hook-stephen-ipad", "hook-stephen-iphone"]


async def test_cards_fall_back_to_the_name_action_without_the_service_object(hass: HomeAssistant, world,
                                                                            monkeypatch) -> None:
    monkeypatch.setattr("custom_components.fenton_home.notify.NOTIFY_SERVICES", None)
    start_fan(hass)
    await settle(hass)
    assert sorted(s["target"] for s in sent(world)) == ["hook-stephen-ipad", "hook-stephen-iphone"]


async def test_a_card_refused_by_mobile_app_is_logged_and_retried(hass: HomeAssistant, world, caplog) -> None:
    original = world.service.async_send_message

    async def refuse(message="", **kwargs):
        if kwargs.get("target") == ["hook-stephen-ipad"]:
            raise HomeAssistantError("Device not connected")
        await original(message, **kwargs)

    world.service.async_send_message = refuse
    start_fan(hass)
    await settle(hass)
    assert "Couldn't send job.fan.guest to hook-stephen-ipad · Fenton: HomeAssistantError" in caplog.text
    assert {s["target"] for s in sent(world)} == {"hook-stephen-iphone"}


async def test_a_device_with_no_notify_action_is_warned_about_once_and_retried(hass: HomeAssistant, world, caplog) -> None:
    mobile_app_entry(hass, "hook-nameless", STEPHEN, app_data=dict(LIVE_DATA))
    world.service.unregistered.add("hook-nameless")
    start_fan(hass)
    await settle(hass)
    hass.states.async_set("input_datetime.guest_bathroom_fan_timer_ends_at", "x", {"timestamp": time.time() + 2400})
    await settle(hass)
    assert caplog.text.count("No notify action for hook-nameless · Fenton") == 1
    assert world.live._wake is not None, "a retry is scheduled"
