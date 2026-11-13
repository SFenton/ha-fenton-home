"""The engine's rules without Home Assistant: lingers, slots, coalescing, restarts, dismissals, problems."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fenton_home.engine import Engine, End, Push, Registration, render, stale_for
from fenton_home.entities import HEARTBEAT_SECONDS, LINGER_SECONDS, MAX_AGE_SECONDS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scenarios as sc  # noqa: E402

NOW = sc.NOW


def run(engine, snapshot, at=0, regs=None):
    return sc.run(engine, snapshot, NOW + at, regs=regs)


def tags(plan, kind=Push):
    return [a.tag for a in plan.actions if isinstance(a, kind)]


# -- Starting, updating, ending ---------------------------------------------------------------------------------------


def test_a_running_job_starts_once_and_is_quiet_while_nothing_changes() -> None:
    engine = Engine()
    plan = run(engine, sc.fan("guest"))
    (push,) = sc.pushes(plan)
    assert push.tag == "job.fan.guest" and push.first and not push.alert and not push.urgent
    assert push.content["phase"] == "running" and push.content["controls"] == [{"id": "stop", "title": "Stop"}]
    assert push.relevance == 67.5 and push.stale_ts == NOW + 1500 + 600
    assert run(engine, sc.fan("guest"), 10).actions == []


def test_a_finished_job_ends_now() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"))
    plan = run(engine, sc.states(), 30)
    (end,) = sc.ends(plan)
    assert end == End("hook-stephen-iphone", "job.fan.guest", "gone")
    assert engine.cards == {}


def test_a_fan_countdown_moves_by_more_than_thirty_seconds_before_it_is_worth_a_push() -> None:
    engine = Engine()
    run(engine, sc.fan("guest", ends_in=1500))
    assert run(engine, sc.fan("guest", ends_in=1520), 10).actions == []
    plan = run(engine, sc.fan("guest", ends_in=1560), 20)
    (push,) = sc.pushes(plan)
    assert not push.first and push.content["value"]["end_ts"] == NOW + 1560


def test_dishwasher_progress_needs_five_points_and_five_minutes() -> None:
    engine = Engine()
    run(engine, sc.dishwasher(finish_in=None, progress=10))
    assert run(engine, sc.dishwasher(finish_in=None, progress=12), 60).actions == []
    assert run(engine, sc.dishwasher(finish_in=None, progress=20), 120).actions == [], "too soon after the start"
    (push,) = sc.pushes(run(engine, sc.dishwasher(finish_in=None, progress=20), 301))
    assert push.content["progress"] == 0.2


def test_a_phase_change_pushes_at_once_but_never_more_than_one_per_two_seconds() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    first = run(engine, sc.main_floor("paused"), 1)
    assert first.actions == [] and first.wake_in is not None and 0 < first.wake_in <= 1.0
    (push,) = sc.pushes(run(engine, sc.main_floor("paused"), 3))
    assert push.content["phase"] == "paused"


def test_a_burst_collapses_to_the_latest_state() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    run(engine, sc.main_floor("paused"), 0.5)
    run(engine, sc.main_floor("cleaning"), 1)
    # Back to what the device already shows: nothing to send.
    assert run(engine, sc.main_floor("cleaning"), 3).actions == []


def test_an_open_ended_card_is_refreshed_before_its_stale_date() -> None:
    engine = Engine()
    plan = run(engine, sc.laundry("dryer"))
    assert plan.wake_in == HEARTBEAT_SECONDS
    assert run(engine, sc.laundry("dryer"), HEARTBEAT_SECONDS - 5).actions == []
    (push,) = sc.pushes(run(engine, sc.laundry("dryer"), HEARTBEAT_SECONDS + 1))
    assert push.stale_ts == NOW + HEARTBEAT_SECONDS + 1 + 1800


def test_a_countdown_card_wakes_the_engine_when_it_runs_out() -> None:
    engine = Engine()
    plan = run(engine, sc.fan("guest", ends_in=100))
    assert plan.wake_in == 101


def test_stale_date() -> None:
    assert stale_for({"value": {"kind": "countdown", "end_ts": NOW + 100}}, NOW) == NOW + 700
    assert stale_for({"value": {"kind": "countdown", "end_ts": NOW - 100}}, NOW) == NOW + 1800
    assert stale_for({"value": {"kind": "text"}}, NOW) == NOW + 1800


# -- Complete lingers -------------------------------------------------------------------------------------------------


def test_the_dishwasher_shows_complete_for_five_minutes_then_ends_and_does_not_come_back() -> None:
    engine = Engine()
    run(engine, sc.dishwasher("run"))
    plan = run(engine, sc.dishwasher("finished"), 30)
    (push,) = sc.pushes(plan)
    assert push.content["phase"] == "done" and push.content["value"] == {"kind": "text", "text": "Complete"}
    assert push.relevance == 10 and plan.wake_in is not None and plan.wake_in <= LINGER_SECONDS
    assert run(engine, sc.dishwasher("finished"), 30 + LINGER_SECONDS - 5).actions == []
    plan = run(engine, sc.dishwasher("finished"), 30 + LINGER_SECONDS + 1)
    assert [e.reason for e in sc.ends(plan)] == ["gone"]
    # Still `finished` a minute later: it must not start again.
    assert run(engine, sc.dishwasher("finished"), 30 + LINGER_SECONDS + 60).actions == []
    # `ready` ends the lingering state; a new run starts a new card.
    run(engine, sc.dishwasher("ready"), 30 + LINGER_SECONDS + 120)
    assert tags(run(engine, sc.dishwasher("run"), 30 + LINGER_SECONDS + 200)) == ["job.dishwasher"]


def test_the_dishwasher_complete_card_ends_when_the_door_opens() -> None:
    engine = Engine()
    run(engine, sc.dishwasher("finished"))
    plan = run(engine, sc.dishwasher("ready"), 60)
    assert [e.reason for e in sc.ends(plan)] == ["gone"]


@pytest.mark.parametrize("job", ["dryer", "washer"])
def test_laundry_completes_when_the_helper_turns_off_and_lingers_five_minutes(job) -> None:
    engine = Engine()
    run(engine, sc.laundry(job))
    plan = run(engine, sc.laundry(job, on=False, on_for=0), 40)
    (push,) = sc.pushes(plan)
    assert push.content["phase"] == "done" and push.content["value"]["text"] == "Complete" and not push.alert
    # The helper stays off; the Complete card holds until its deadline, then ends.
    assert run(engine, sc.laundry(job, on=False, on_for=0), 200).actions == []
    assert [e.reason for e in sc.ends(run(engine, sc.laundry(job, on=False, on_for=0), 40 + LINGER_SECONDS + 2))] == ["gone"]
    assert run(engine, sc.laundry(job, on=False, on_for=0), 40 + LINGER_SECONDS + 100).actions == []


def test_a_laundry_cycle_that_starts_again_is_a_fresh_card_with_its_own_complete() -> None:
    engine = Engine()
    run(engine, sc.laundry("dryer"))
    run(engine, sc.laundry("dryer", on=False, on_for=0), 40)
    run(engine, sc.laundry("dryer"), 400)
    plan = run(engine, sc.laundry("dryer", on=False, on_for=0), 4000)
    (push,) = sc.pushes(plan)
    assert push.content["phase"] == "done"


def test_laundry_that_is_off_at_startup_shows_nothing() -> None:
    assert run(Engine(), sc.laundry("dryer", on=False)).actions == []


def test_hot_flash_complete_lingers_through_idle_then_ends() -> None:
    engine = Engine()
    run(engine, sc.hot_flash("holding", "idle"))
    plan = run(engine, sc.hot_flash("restore_pending", "idle"), 900)
    (push,) = sc.pushes(plan)
    assert push.content["value"]["text"] == "Complete" and push.relevance == 10
    assert run(engine, sc.hot_flash("idle", "idle"), 960).actions == [], "idle must not cut the Complete short"
    assert [e.reason for e in sc.ends(run(engine, sc.hot_flash("idle", "idle"), 900 + LINGER_SECONDS + 2))] == ["gone"]


def test_a_stopped_job_ends_at_once_with_no_complete() -> None:
    engine = Engine()
    run(engine, sc.hot_flash("holding", "idle"))
    plan = run(engine, sc.hot_flash("idle", "idle"), 60)
    assert sc.pushes(plan) == [] and [e.reason for e in sc.ends(plan)] == ["gone"]
    engine = Engine()
    run(engine, sc.hot_flash("holding", "idle"))
    assert [e.reason for e in sc.ends(run(engine, sc.hot_flash("superseded", "idle"), 60))] == ["gone"]


# -- Vacuum sessions --------------------------------------------------------------------------------------------------


def test_a_new_room_ends_the_card_and_starts_another_on_the_same_tag() -> None:
    engine = Engine()
    run(engine, sc.main_floor(room_id="kitchen"))
    plan = run(engine, sc.main_floor(room_id="hall").with_(
        sensor__main_floor_vacuum_coordinator_hall_last_cleaned=("x", {"room_name": "Hall"})), 600)
    assert [type(a) for a in plan.actions] == [End, Push]
    end, push = plan.actions
    assert end.reason == "restart" and push.first and push.content["headline"] == "Cleaning Hall"


def test_a_room_that_finishes_ends_the_card_until_the_next_one_starts() -> None:
    engine = Engine()
    run(engine, sc.main_floor(room_id="kitchen"))
    between = run(engine, sc.main_floor("cleaning", room_id="", phase="idle"), 700)
    assert [e.reason for e in sc.ends(between)] == ["gone"]
    assert tags(run(engine, sc.main_floor(room_id="hall"), 720)) == ["job.vacuum.main_floor"]


def test_pausing_and_resuming_updates_the_same_card() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    (paused,) = sc.pushes(run(engine, sc.main_floor("paused", changed=NOW + 5), 5))
    assert paused.content["phase"] == "paused" and paused.content["headline"] == "Paused in Kitchen"
    assert paused.content["short"] == "Paused"
    (resumed,) = sc.pushes(run(engine, sc.main_floor("cleaning"), 20))
    assert resumed.content["phase"] == "running" and not resumed.first and resumed.content["short"] == "Kitchen"


def test_a_vacuum_card_has_no_value_and_no_progress_but_a_short_word() -> None:
    (push,) = sc.pushes(run(Engine(), sc.main_floor()))
    assert "value" not in push.content and "progress" not in push.content
    assert push.content["headline"] == "Cleaning Kitchen" and push.content["short"] == "Kitchen"
    assert push.stale_ts == NOW + 1800
    # A problem that rides on a vacuum tag is the usual problem card, with its text value.
    (problem,) = sc.pushes(run(Engine(), sc.main_floor_issue("navigation.stuck")))
    assert problem.content["value"] == {"kind": "text", "text": "Needs help"} and "short" not in problem.content


# -- A manual vacuum run's room flips ------------------------------------------------------------------------------------


def replay_music_room_run(engine: Engine) -> list:
    """Run ``sc.MUSIC_ROOM_RUN`` through the engine, also waking whenever the plan asks, to the end of the run.

    Returns ``(seconds, action)`` for every start and end the engine plans.
    """
    log: list = []
    changes = [(at, step) for step, (at, *_rest) in enumerate(sc.MUSIC_ROOM_RUN)]
    snapshot, wake_at, now = None, None, 0.0
    while changes or wake_at is not None:
        due = [changes[0][0]] if changes else []
        if wake_at is not None:
            due.append(wake_at)
        now = min(due)
        while changes and changes[0][0] <= now:
            snapshot = sc.music_room_run(changes.pop(0)[1])
        plan = run(engine, snapshot, now)
        log.extend((round(now, 3), a) for a in plan.actions if isinstance(a, (Push, End)))
        wake_at = None if plan.wake_in is None else now + plan.wake_in
    return log


def test_the_recorded_music_room_run_starts_one_card_per_room_that_held() -> None:
    log = replay_music_room_run(Engine())
    starts = [(at, a.content["headline"]) for at, a in log if isinstance(a, Push) and a.first]
    assert [headline for _, headline in starts] == ["Cleaning Music Room", "Vacuuming Pantry", "Vacuuming Hall",
                                                    "Vacuuming Music Room"]
    # Each new room waits for the sensors to read it for a minute; the 20 s back in the first room never had a card.
    assert [round(at) for at, _ in starts] == [0, 126, 756, 1206]
    ends = [(round(at), a.reason) for at, a in log if isinstance(a, End)]
    assert ends == [(126, "restart"), (756, "restart"), (1206, "restart"), (6811, "gone")]


def test_the_status_flag_catching_up_updates_the_card_in_place() -> None:
    """03:00:00.592 the vacuum read ``cleaning`` and 8 ms later its flag read ``segment``: that was one start, one end
    and a second start within half a second."""
    engine = Engine()
    (start,) = sc.pushes(run(engine, sc.music_room_run(0), 0))
    assert start.first and start.content["headline"] == "Cleaning Music Room"
    plan = run(engine, sc.music_room_run(1), 0.008)
    assert sc.ends(plan) == [] and not any(p.first for p in sc.pushes(plan))
    (update,) = sc.pushes(run(engine, sc.music_room_run(1), 3))  # the coalescing window (2 s) has passed
    assert not update.first and update.content["headline"] == "Vacuuming Music Room"
    assert next(iter(engine.cards.values())).session_key == "none:"


def test_a_room_that_does_not_hold_for_the_window_is_never_shown() -> None:
    engine = Engine()
    run(engine, sc.manual_run("music_room", flag="segment", segment="Music Room"), 0)
    for at, room in ((100, "Pantry"), (159, "Music Room")):  # in the Pantry for 59 s: not a room change
        plan = run(engine, sc.manual_run("music_room", flag="segment", segment=room), at)
        assert sc.ends(plan) == [] and not any(p.first for p in sc.pushes(plan))
    assert run(engine, sc.manual_run("music_room", flag="segment", segment="Music Room"), 400).actions == []
    held = run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry"), 500)
    assert sc.ends(held) == [] and held.wake_in == pytest.approx(60)
    assert [type(a) for a in run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry"), 560).actions] \
        == [End, Push]


def test_while_a_room_change_waits_the_card_keeps_its_room_and_follows_the_robot() -> None:
    engine = Engine()
    run(engine, sc.manual_run("music_room", flag="segment", segment="Music Room"), 0)
    plan = run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry"), 100)
    assert plan.actions == []
    (paused,) = sc.pushes(run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry", vac="paused"), 110))
    assert paused.content["phase"] == "paused" and not paused.first
    assert next(iter(engine.cards.values())).session_key == "segment:Music Room"


def test_a_run_that_ends_while_a_room_change_waits_leaves_nothing_behind() -> None:
    engine = Engine()
    run(engine, sc.manual_run("music_room", flag="segment", segment="Music Room"), 0)
    run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry"), 100)
    assert [e.reason for e in sc.ends(run(engine, sc.manual_run("music_room", vac="returning"), 120))] == ["gone"]
    assert engine.settling == {}
    (again,) = sc.pushes(run(engine, sc.manual_run("music_room", flag="segment", segment="Pantry"), 900))
    assert again.first and again.content["headline"] == "Vacuuming Pantry"


def test_a_robot_with_a_coordinator_still_changes_card_at_once() -> None:
    """Only the position estimate of a robot with no coordinator needs settling (section 0 decision 5 is unchanged)."""
    engine = Engine()
    run(engine, sc.main_floor(room_id="kitchen"))
    plan = run(engine, sc.main_floor(room_id="hall").with_(
        sensor__main_floor_vacuum_coordinator_hall_last_cleaned=("x", {"room_name": "Hall"})), 90)
    assert [type(a) for a in plan.actions] == [End, Push] and engine.settling == {}


# -- Restart at 7 h 45 -----------------------------------------------------------------------------------------------


def test_a_card_that_outlives_eight_hours_is_ended_and_started_again() -> None:
    engine = Engine()
    run(engine, sc.laundry("dryer", on_for=100))
    plan = run(engine, sc.laundry("dryer", on_for=100), MAX_AGE_SECONDS + 5)
    assert [type(a) for a in plan.actions] == [End, Push]
    assert plan.actions[0].reason == "restart" and plan.actions[1].first
    assert run(engine, sc.laundry("dryer", on_for=100), MAX_AGE_SECONDS + 10).actions == []


# -- Slots ------------------------------------------------------------------------------------------------------------


def five_jobs() -> object:
    return sc.fan("guest").with_(**{
        "input_boolean__master_bathroom_fan_timer_pending": ("on", {}, NOW - 60),
        "input_datetime__master_bathroom_fan_timer_ends_at": ("x", {"timestamp": NOW + 900}),
        "sensor__dishwasher_operation_state": ("run", {}, NOW - 100),
        "sensor__dishwasher_program_finish_time": sc.iso(NOW + 4000),
        "input_boolean__dryer_started_helper": ("on", {}, NOW - 100),
        "input_boolean__washer_started_helper": ("on", {}, NOW - 100),
    })


def test_a_fifth_job_waits_for_a_slot_and_starts_when_one_frees() -> None:
    engine = Engine()
    plan = run(engine, five_jobs())
    started = set(tags(plan))
    assert len(started) == 4
    assert len(engine.cards) == 4
    queued = ({"job.fan.guest", "job.fan.master", "job.dishwasher", "job.dryer", "job.washer"} - started).pop()
    # One finishes; the queued job takes the slot.
    without_guest = five_jobs().with_(input_boolean__guest_bathroom_fan_timer_pending="off")
    plan = run(engine, without_guest, 30)
    assert queued in tags(plan) or queued in {a.tag for a in plan.actions}


def test_a_critical_problem_bumps_the_least_relevant_job_when_every_slot_is_used() -> None:
    engine = Engine()
    four = five_jobs().with_(input_boolean__washer_started_helper="off")
    first_problem = four.with_(sensor__music_room_vacuum_status=("error", {"current_issue": sc.issue(
        "vendor.unknown_error_12", "Unknown error 12")}), vacuum__valetudo_elatedusedram=("error", {}, NOW))
    run(engine, first_problem)
    assert len(engine.cards) == 5, "four jobs and one problem"
    visible_before = {tag for (_, tag) in engine.cards}
    second = first_problem.with_(**{"vacuum__valetudo_exaltedsneakydeer": ("error", {}, NOW),
                                    "sensor__main_floor_vacuum_status": ("error", {
                                        "current_issue": sc.issue("navigation.stuck")})})
    plan = run(engine, second, 30)
    pushed = sc.pushes(plan)
    assert [p.tag for p in pushed] == ["job.vacuum.main_floor"] and pushed[0].alert
    (bumped,) = sc.ends(plan)
    assert bumped.reason == "bumped" and bumped.tag in visible_before
    assert bumped.tag == "job.dishwasher", "the one with the least relevance (the latest finish)"
    assert len(engine.cards) == 5
    # The bumped job is not lost: it starts again when a slot frees.
    freed = second.with_(sensor__music_room_vacuum_status=("clear", {"current_issue": sc.CLEAR}),
                         vacuum__valetudo_elatedusedram=("docked", {}, NOW + 40))
    assert bumped.tag in tags(run(engine, freed, 90))


def test_a_problem_takes_the_fifth_slot_without_ending_anything_when_only_four_jobs_run() -> None:
    engine = Engine()
    four = sc.fan("guest").with_(**{
        "input_boolean__master_bathroom_fan_timer_pending": ("on", {}, NOW - 60),
        "input_datetime__master_bathroom_fan_timer_ends_at": ("x", {"timestamp": NOW + 900}),
        "input_boolean__dryer_started_helper": ("on", {}, NOW - 100),
        "input_boolean__washer_started_helper": ("on", {}, NOW - 100),
    })
    run(engine, four)
    assert len(engine.cards) == 4
    plan = run(engine, four.with_(sensor__dishwasher_operation_state="error"), 30)
    assert tags(plan) == ["job.dishwasher"] and sc.ends(plan) == []


def test_a_device_with_a_job_and_a_problem_keeps_one_card() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    plan = run(engine, sc.main_floor("paused", **{"sensor__main_floor_vacuum_status": (
        "error", {"current_issue": {"code": "mop.clean_water_empty", "status": "active", "since": sc.iso(NOW)}})}), 30)
    (push,) = sc.pushes(plan)
    assert push.tag == "job.vacuum.main_floor" and not push.first
    assert push.content["phase"] == "problem" and push.content["problem"]["severity"] == "warning"
    assert not push.alert, "warnings never alert"
    assert len(engine.cards) == 1


# -- Audience ---------------------------------------------------------------------------------------------------------


def test_every_person_with_a_registration_gets_the_household_jobs_and_hot_flash_only_its_audience() -> None:
    engine = Engine()
    snapshot = sc.fan("guest").with_(**{
        "input_select__sleepypod_hot_flash_left_phase": "holding",
        "timer__sleepypod_hot_flash_left": ("active", {"finishes_at": sc.iso(NOW + 500)}),
        "input_text__eight_sleep_stephen_hot_flash_requested_by": "stephen",
    })
    plan = run(engine, snapshot, regs=[sc.PHONE, sc.IPAD, sc.STEPH_PHONE])
    by_device: dict[str, set[str]] = {}
    for push in sc.pushes(plan):
        by_device.setdefault(push.webhook_id, set()).add(push.tag)
    assert by_device["hook-stephen-iphone"] == {"job.fan.guest", "job.hot_flash"}
    assert by_device["hook-stephen-ipad"] == {"job.fan.guest", "job.hot_flash"}
    assert by_device["hook-steph-iphone"] == {"job.fan.guest"}


def test_cards_per_person_differ_in_content() -> None:
    engine = Engine()
    snapshot = sc.hot_flash("holding", "idle", by_left="steph")
    plan = run(engine, snapshot, regs=[sc.PHONE, sc.STEPH_PHONE])
    lines = {p.webhook_id: p.content["detail"] for p in sc.pushes(plan)}
    assert lines == {"hook-stephen-iphone": "Your side · by Steph", "hook-steph-iphone": "Stephen's side"}
    assert all("headline" not in p.content for p in sc.pushes(plan))


def test_a_registration_with_no_person_gets_only_the_household_jobs() -> None:
    engine = Engine()
    loose = Registration("hook-loose", "user-loose", "Loose · Fenton", None)
    snapshot = sc.fan("guest").with_(input_select__sleepypod_hot_flash_left_phase="holding")
    plan = run(engine, snapshot, regs=[loose])
    assert tags(plan) == ["job.fan.guest"]


def test_a_registration_that_stops_being_eligible_loses_its_cards() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"), regs=[sc.PHONE, sc.IPAD])
    plan = run(engine, sc.fan("guest"), 10, regs=[sc.PHONE])
    assert [(e.webhook_id, e.reason) for e in sc.ends(plan)] == [("hook-stephen-ipad", "not eligible")]


# -- A registration without a push-to-start token ----------------------------------------------------------------------


def no_start(registration: Registration) -> Registration:
    """The same registration after an ``update_registration`` that left out ``start_live_activity_token``."""
    return Registration(registration.webhook_id, registration.user_id, registration.name, registration.person,
                        can_start=False)


def test_a_registration_that_loses_its_start_token_keeps_updating_and_ending_the_cards_it_shows() -> None:
    engine = Engine()
    run(engine, sc.fan("guest", ends_in=1500), regs=[sc.PHONE, sc.IPAD])
    regs = [sc.PHONE, no_start(sc.IPAD)]
    plan = run(engine, sc.fan("guest", ends_in=1700), 10, regs=regs)
    assert {p.webhook_id for p in sc.pushes(plan)} == {"hook-stephen-iphone", "hook-stephen-ipad"}
    assert sc.ends(plan) == []
    plan = run(engine, sc.states(), 30, regs=regs)
    assert {(e.webhook_id, e.reason) for e in sc.ends(plan)} == {("hook-stephen-iphone", "gone"),
                                                                 ("hook-stephen-ipad", "gone")}
    assert engine.cards == {}


def test_a_registration_without_a_start_token_is_not_started_on_a_new_card() -> None:
    engine = Engine()
    plan = run(engine, sc.fan("guest"), regs=[sc.PHONE, no_start(sc.IPAD)])
    assert [p.webhook_id for p in sc.pushes(plan)] == ["hook-stephen-iphone"]
    assert set(engine.cards) == {("hook-stephen-iphone", "job.fan.guest")}
    # It begins the card on the next pass once the app hands the token over.
    plan = run(engine, sc.fan("guest"), 5, regs=[sc.PHONE, sc.IPAD])
    assert [(p.webhook_id, p.first) for p in sc.pushes(plan)] == [("hook-stephen-ipad", True)]


def test_a_card_the_device_cannot_restart_ends_and_waits_for_the_token() -> None:
    engine = Engine()
    run(engine, sc.laundry("dryer", on_for=100), regs=[sc.PHONE])
    plan = run(engine, sc.laundry("dryer", on_for=100), MAX_AGE_SECONDS + 5, regs=[no_start(sc.PHONE)])
    assert [(type(a), a.reason) for a in plan.actions] == [(End, "restart")]
    plan = run(engine, sc.laundry("dryer", on_for=100), MAX_AGE_SECONDS + 10, regs=[sc.PHONE])
    assert [(p.first) for p in sc.pushes(plan)] == [True]


def test_only_a_registration_that_is_gone_is_ended_as_not_eligible() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"), regs=[sc.PHONE, sc.IPAD])
    plan = run(engine, sc.fan("guest"), 10, regs=[no_start(sc.PHONE), no_start(sc.IPAD)])
    assert plan.actions == []
    plan = run(engine, sc.fan("guest"), 20, regs=[no_start(sc.PHONE)])
    assert [(e.webhook_id, e.reason) for e in sc.ends(plan)] == [("hook-stephen-ipad", "not eligible")]


# -- Dismissals -------------------------------------------------------------------------------------------------------


def test_a_dismissed_card_is_not_restarted_until_the_job_ends() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"))
    assert engine.dismiss("hook-stephen-iphone", "job.fan.guest", NOW + 60)
    assert run(engine, sc.fan("guest", ends_in=2400), 90).actions == []
    run(engine, sc.states(), 120)
    assert tags(run(engine, sc.fan("guest"), 200)) == ["job.fan.guest"], "a new run starts a new card"


def test_a_dismissal_only_reaches_the_device_that_did_it() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"), regs=[sc.PHONE, sc.IPAD])
    engine.dismiss("hook-stephen-iphone", "job.fan.guest", NOW + 60)
    plan = run(engine, sc.fan("guest", ends_in=2400), 90, regs=[sc.PHONE, sc.IPAD])
    assert [p.webhook_id for p in sc.pushes(plan)] == ["hook-stephen-ipad"]


def test_the_apps_own_report_of_our_end_is_not_a_dismissal() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"))
    run(engine, sc.states(), 30)  # we end it
    assert not engine.dismiss("hook-stephen-iphone", "job.fan.guest", NOW + 31)
    # Nor is a report for a card we never showed there.
    assert not engine.dismiss("hook-other", "job.fan.guest", NOW + 31)
    run(engine, sc.fan("guest"), 100)
    assert engine.dismiss("hook-stephen-iphone", "job.fan.guest", NOW + 200)


def test_a_problem_that_turns_critical_after_a_dismissal_starts_again() -> None:
    engine = Engine()
    warning = sc.main_floor_issue("dock.cover_open")
    plan = run(engine, warning)
    (push,) = sc.pushes(plan)
    assert push.content["problem"]["severity"] == "warning"
    engine.dismiss("hook-stephen-iphone", "job.vacuum.main_floor", NOW + 60)
    assert run(engine, warning, 90).actions == []
    critical = run(engine, sc.main_floor_issue("navigation.stuck"), 120)
    (push,) = sc.pushes(critical)
    assert push.first and push.alert and push.content["problem"]["severity"] == "critical"


def test_a_dismissed_critical_problem_stays_away() -> None:
    engine = Engine()
    run(engine, sc.main_floor_issue("navigation.stuck"))
    engine.dismiss("hook-stephen-iphone", "job.vacuum.main_floor", NOW + 60)
    assert run(engine, sc.main_floor_issue("navigation.stuck"), 90).actions == []


# -- Problems ---------------------------------------------------------------------------------------------------------


def test_a_critical_problem_alerts_once_when_it_turns_the_card_red() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    (push,) = sc.pushes(run(engine, sc.main_floor_issue("navigation.stuck", vac="paused"), 20))
    assert push.alert and push.content["phase"] == "problem" and push.relevance == 100
    # Same problem, new detail: no second alert.
    renamed = sc.main_floor_issue("navigation.stuck", vac="paused").with_(
        sensor__main_floor_vacuum_coordinator_kitchen_last_cleaned=("x", {"room_name": "Galley"}))
    (again,) = sc.pushes(run(engine, renamed, 40))
    assert not again.alert and not again.first and again.content["problem"]["problem"] == "Stuck in Galley"
    # Clearing never alerts: back to the job card.
    (cleared,) = sc.pushes(run(engine, sc.main_floor(), 80))
    assert cleared.content["phase"] == "running" and not cleared.alert


def test_a_problem_with_no_job_starts_the_devices_tag_and_ends_when_it_clears() -> None:
    engine = Engine()
    plan = run(engine, sc.main_floor_issue("dock.dustbag_blocked", vac="docked"))
    (push,) = sc.pushes(plan)
    assert push.first and push.relevance == 90 and push.content["problem"]["icon"] == "bag"
    assert [e.reason for e in sc.ends(run(engine, sc.main_floor("docked", room_id="", phase="idle", auto=False), 60))] == ["gone"]


def test_an_unmapped_error_is_quoted_logged_once_and_has_no_chips_or_done(caplog) -> None:
    engine = Engine()
    with caplog.at_level("WARNING"):
        (push,) = sc.pushes(run(engine, sc.robot_error("Wheel tangled in a cable")))
        run(engine, sc.robot_error("Wheel tangled in a cable"), 10)
    problem = push.content["problem"]
    assert problem["message"] == 'The robot says "Wheel tangled in a cable". Check it, then tap Resume.'
    assert problem["chips"] == [] and "task_id" not in problem
    assert [c["id"] for c in push.content["controls"]] == ["resume", "dock"]
    assert caplog.text.count("fenton_home.problem_unmapped") == 1


def test_a_service_raised_problem_with_a_task_stays_while_the_device_runs_no_job() -> None:
    engine = Engine()
    engine.raise_problem("vacuum.main_floor", "navigation.stuck", None, NOW, task_title="Main Floor Vacuum · Robot Stuck",
                         push_tag="vacuum-kitchen-error")
    record = engine.records["vacuum.main_floor"]
    record.task_id = 4217
    (push,) = sc.pushes(run(engine, sc.main_floor_issue("navigation.stuck")))
    assert [c["id"] for c in push.content["controls"]] == ["resume", "dock", "done"]
    assert push.content["problem"]["task_id"] == 4217
    # The robot is docked and the issue is clear, but no job runs and the task is open: the problem card stays.
    docked = sc.main_floor("docked", room_id="", phase="idle", auto=False)
    still = run(engine, docked, 60)
    assert all(p.content["phase"] == "problem" for p in sc.pushes(still))
    assert sc.ends(still) == []
    # The task is completed (by Home Assistant, a person or Done): the card ends everywhere.
    record.task_completed = True
    plan = run(engine, docked, 120)
    assert [e.reason for e in sc.ends(plan)] == ["gone"]
    assert engine.records == {}
    assert [r.push_tag for r in engine.take_ended_records()] == ["vacuum-kitchen-error"]


DOCKED = sc.main_floor("docked", room_id="", phase="idle", auto=False)


def tasked(engine: Engine, job_id: str, code: str, tag: str = "problem-tag") -> None:
    engine.raise_problem(job_id, code, None, NOW, task_title="A task", push_tag=tag)
    engine.records[job_id].task_id = 4217


def test_a_vacuum_that_stopped_with_a_problem_and_resumed_ends_its_card_when_it_runs_again() -> None:
    """The reported case. Fails on main (the card stayed a problem card) and on the first version (one reading ended it)."""
    engine = Engine()
    tasked(engine, "vacuum.main_floor", "mop.clean_water_empty", "vacuum-clean-water")
    (push,) = sc.pushes(run(engine, sc.main_floor_issue("mop.clean_water_empty")))
    assert push.content["phase"] == "problem"
    # Docked, the error already cleared (it clears seconds after the dock): the card stays.
    assert all(p.content["phase"] == "problem" for p in sc.pushes(run(engine, DOCKED, 20)))
    # Cleaning again with no issue: one reading is not enough.
    assert all(p.content["phase"] == "problem" for p in sc.pushes(run(engine, sc.main_floor(), 60)))
    assert "vacuum.main_floor" in engine.records
    # Still cleaning half a minute later: the job card returns and the task stays open.
    (running,) = sc.pushes(run(engine, sc.main_floor(), 100))
    assert running.content["phase"] == "running" and not running.alert
    assert engine.records == {} and engine.take_ended_records()[0].task_completed is False
    # The job finishes like any other job.
    assert [e.reason for e in sc.ends(run(engine, DOCKED, 900))] == ["gone"]


def test_a_vacuum_problem_raised_while_it_cleans_with_no_issue_reported_stays_for_its_task() -> None:
    """Fails without the narrowing: the card ended within a minute although the sensors never reported the problem."""
    engine = Engine()
    tasked(engine, "vacuum.main_floor", "navigation.stuck")
    for at in (0, 60, 200, 600):
        plan = run(engine, sc.main_floor(), at)
        assert all(p.content["phase"] == "problem" for p in sc.pushes(plan)) and sc.ends(plan) == []
    assert "vacuum.main_floor" in engine.records


def test_a_dishwasher_problem_with_no_sensor_raised_during_a_run_stays_for_its_task() -> None:
    """Fails without the narrowing: a rinse aid problem raised during a run ended once its task was linked."""
    engine = Engine()
    tasked(engine, "dishwasher", "dishwasher.rinse_aid")
    for at in (0, 60, 200):
        plan = run(engine, sc.dishwasher("run"), at)
        assert all(p.content["phase"] == "problem" for p in sc.pushes(plan)) and sc.ends(plan) == []
    assert "dishwasher" in engine.records


def test_a_paused_vacuum_whose_issue_clears_keeps_its_card_until_it_runs() -> None:
    """Fails without the narrowing: paused counted as running and the card ended before Resume."""
    engine = Engine()
    tasked(engine, "vacuum.main_floor", "mop.clean_water_empty")
    run(engine, sc.main_floor_issue("mop.clean_water_empty", vac="paused"))
    for at in (20, 80, 200):
        assert all(p.content["phase"] == "problem" for p in sc.pushes(run(engine, sc.main_floor("paused"), at)))
    assert "vacuum.main_floor" in engine.records
    run(engine, sc.main_floor(), 300)
    (push,) = sc.pushes(run(engine, sc.main_floor(), 340))
    assert push.content["phase"] == "running" and engine.records == {}


def test_a_paused_dishwasher_whose_problem_clears_keeps_its_card_until_it_runs() -> None:
    """Fails without the narrowing: a paused dishwasher counted as running."""
    engine = Engine()
    tasked(engine, "dishwasher", "dishwasher.action_required")
    run(engine, sc.states(sensor__dishwasher_operation_state=("actionrequired", {}, NOW - 60)))
    for at in (20, 80, 200):
        assert all(p.content["phase"] == "problem" for p in sc.pushes(run(engine, sc.dishwasher("pause"), at)))
    assert "dishwasher" in engine.records
    run(engine, sc.dishwasher("run"), 300)
    (push,) = sc.pushes(run(engine, sc.dishwasher("run"), 340))
    assert push.content["phase"] == "running" and engine.records == {}


def test_one_clear_reading_does_not_end_a_card_and_an_unknown_history_waits_for_the_task() -> None:
    """Fails without the confirmation and the history: a single snapshot, or a stored record with no history, ended it."""
    engine = Engine()
    tasked(engine, "vacuum.main_floor", "navigation.stuck")
    run(engine, sc.main_floor_issue("navigation.stuck"))
    run(engine, DOCKED, 20)
    run(engine, sc.main_floor(), 60)  # one clear reading while it runs
    plan = run(engine, sc.main_floor_issue("navigation.stuck"), 70)  # the issue comes back
    assert "vacuum.main_floor" in engine.records
    assert all(p.content["phase"] == "problem" for p in sc.pushes(plan))
    # A restart: the record is back from the store with no history (an older record), and the robot is cleaning.
    restarted = Engine()
    tasked(restarted, "vacuum.main_floor", "navigation.stuck")
    restarted.records["vacuum.main_floor"].sensor_seen = True
    for at in (0, 60, 300):
        run(restarted, sc.main_floor(), at)
    assert "vacuum.main_floor" in restarted.records and not restarted.records["vacuum.main_floor"].seen_idle
    # Within the start-up hold nothing ends, even with the history known.
    held = Engine()
    tasked(held, "vacuum.main_floor", "navigation.stuck")
    held.records["vacuum.main_floor"].sensor_seen = held.records["vacuum.main_floor"].seen_idle = True
    held.hold_until = NOW + 500
    for at in (0, 60, 300):
        run(held, sc.main_floor(), at)
    assert "vacuum.main_floor" in held.records


def test_a_problem_that_still_shows_its_issue_stays_a_problem_while_the_device_runs() -> None:
    engine = Engine()
    tasked(engine, "vacuum.main_floor", "navigation.stuck")
    run(engine, sc.main_floor_issue("navigation.stuck"))
    # Cleaning, but the sensors still report the issue: the problem is not over.
    for at in (60, 200):
        plan = run(engine, sc.main_floor_issue("navigation.stuck", vac="cleaning"), at)
        assert all(p.content["phase"] == "problem" for p in sc.pushes(plan)) and sc.ends(plan) == []
    assert "vacuum.main_floor" in engine.records


def test_done_on_a_robot_that_is_still_stuck_does_not_bring_the_card_straight_back() -> None:
    engine = Engine()
    engine.raise_problem("vacuum.main_floor", "navigation.stuck", None, NOW, task_title="Main Floor Vacuum · Robot Stuck")
    engine.records["vacuum.main_floor"].task_id = 4217
    stuck = sc.main_floor_issue("navigation.stuck")
    run(engine, stuck)
    engine.records["vacuum.main_floor"].task_completed = True
    plan = run(engine, stuck, 30)
    assert [e.reason for e in sc.ends(plan)] == ["gone"] and sc.pushes(plan) == []
    assert run(engine, stuck, 60).actions == [] and engine.records == {}
    # The robot recovers; if it gets stuck again that is a new problem and a new card.
    run(engine, sc.main_floor(), 90)
    assert [p.alert for p in sc.pushes(run(engine, stuck, 120))] == [True]
    # A fresh service call for the same code also brings it back at once.
    engine.records.clear()
    run(engine, sc.states(), 150)
    engine.suppressed["vacuum.main_floor"] = "navigation.stuck"
    engine.raise_problem("vacuum.main_floor", "navigation.stuck", None, NOW + 160)
    assert engine.suppressed == {}


def test_a_service_raised_problem_without_a_task_ends_when_the_sensor_clears() -> None:
    engine = Engine()
    engine.raise_problem("vacuum.main_floor", "power.low_battery", None, NOW)
    run(engine, sc.main_floor_issue("power.low_battery", vac="returning"))
    assert engine.records["vacuum.main_floor"].sensor_seen
    run(engine, sc.main_floor("docked", room_id="", phase="idle", auto=False), 60)
    assert engine.records == {}


def test_a_service_problem_the_sensors_never_confirm_is_kept_for_a_grace_period_only() -> None:
    engine = Engine()
    engine.raise_problem("dishwasher", "dishwasher.rinse_aid", None, NOW)
    assert tags(run(engine, sc.states())) == ["job.dishwasher"]
    assert engine.records
    run(engine, sc.states(), 300)
    assert engine.records == {}


def test_a_service_problem_waits_for_its_task_to_be_found_but_not_forever() -> None:
    engine = Engine()
    engine.raise_problem("dishwasher", "dishwasher.rinse_aid", None, NOW, task_title="Refill rinse aid")
    run(engine, sc.states(), 30)
    assert "dishwasher" in engine.records, "lookups are still pending"
    run(engine, sc.states(), 200)
    assert engine.records == {}, "the task never turned up within the lookup window, and the grace period is over"
    engine.raise_problem("dishwasher", "dishwasher.rinse_aid", None, NOW + 100, task_title="Refill rinse aid")
    engine.records["dishwasher"].lookups = 5
    run(engine, sc.states(), 400)
    assert engine.records == {}, "or within five lookups"


def test_the_service_code_wins_over_a_different_sensor_code_while_the_problem_lasts() -> None:
    engine = Engine()
    engine.raise_problem("vacuum.main_floor", "power.critical_battery", None, NOW)
    (push,) = sc.pushes(run(engine, sc.main_floor_issue("power.low_battery", vac="returning")))
    assert push.content["problem"]["code"] == "power.critical_battery"


def test_a_sensor_problem_that_changes_code_replaces_its_record() -> None:
    engine = Engine()
    run(engine, sc.main_floor_issue("navigation.stuck"))
    (push,) = sc.pushes(run(engine, sc.main_floor_issue("maintenance.main_brush_jammed"), 30))
    assert push.content["problem"]["code"] == "maintenance.main_brush_jammed"
    assert [r.code for r in engine.take_ended_records()] == ["navigation.stuck"]


# -- Notices, confirmations and failures ------------------------------------------------------------------------------


def test_a_notice_rides_one_update_for_the_person_who_pressed() -> None:
    engine = Engine()
    run(engine, sc.main_floor(), regs=[sc.PHONE, sc.STEPH_PHONE])
    engine.set_notice("job.vacuum.main_floor", sc.USER_STEPHEN, "Couldn't pause. Try again.", NOW)
    engine.mark_urgent("job.vacuum.main_floor")
    plan = run(engine, sc.main_floor(), 5, regs=[sc.PHONE, sc.STEPH_PHONE])
    (push,) = sc.pushes(plan)
    assert push.webhook_id == "hook-stephen-iphone" and push.urgent
    assert push.content["notice"] == "Couldn't pause. Try again."
    again = run(engine, sc.main_floor(), 20, regs=[sc.PHONE, sc.STEPH_PHONE])
    assert again.actions == [], "the notice is gone and the content is what the device already shows"


def test_an_urgent_mark_survives_until_a_push_uses_it() -> None:
    engine = Engine()
    run(engine, sc.main_floor())
    engine.mark_urgent("job.vacuum.main_floor")
    assert run(engine, sc.main_floor(), 1).actions == []
    (push,) = sc.pushes(run(engine, sc.main_floor("paused"), 3))
    assert push.urgent
    assert not sc.pushes(run(engine, sc.main_floor("cleaning"), 10))[0].urgent


def test_a_failed_push_is_sent_again() -> None:
    engine = Engine()
    (push,) = sc.pushes(run(engine, sc.fan("guest")))
    engine.push_failed(push.webhook_id, push.tag, NOW)
    assert tags(run(engine, sc.fan("guest"), 5)) == ["job.fan.guest"]


def test_render_orders_controls_and_marks_the_second_secondary() -> None:
    engine = Engine()
    engine.refresh(sc.main_floor("paused"), NOW, sc.ctx())
    content = render(engine.current[0])
    assert content["controls"] == [{"id": "resume", "title": "Resume"}, {"id": "dock", "title": "Dock", "role": "secondary"}]
    assert content["phase"] == "paused" and content["v"] == 2


def test_sensor_description_lists_tags_and_devices() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"))
    description = engine.describe()
    assert description["job.fan.guest"]["visible_on"] == ["hook-stephen-iphone"]
    assert description["job.fan.guest"]["phase"] == "running"


def test_cards_with_no_job_are_kept_through_the_startup_hold_then_ended() -> None:
    engine = Engine()
    run(engine, sc.fan("guest"))
    engine.hold_until = NOW + 120
    held = run(engine, sc.states(), 10)
    assert held.actions == [] and held.wake_in is not None and 100 < held.wake_in <= 110
    assert ("hook-stephen-iphone", "job.fan.guest") in engine.cards
    # A card the engine still wants is updated as usual during the hold; a bump or an ineligible device still ends.
    assert [e.reason for e in sc.ends(run(engine, sc.states(), 130))] == ["gone"]
    engine = Engine()
    run(engine, sc.fan("guest"), regs=[sc.PHONE, sc.IPAD])
    engine.hold_until = NOW + 120
    assert [e.reason for e in sc.ends(run(engine, sc.states(), 10, regs=[sc.PHONE]))] == ["not eligible"]


def test_fan_stop_can_cancel_the_timer_first(monkeypatch) -> None:
    from fenton_home import controls  # noqa: PLC0415

    assert len(controls.fan_stop("guest").calls) == 1
    monkeypatch.setattr(controls, "FAN_STOP_CANCELS_TIMER_FIRST", True)
    calls = controls.fan_stop("guest").calls
    assert [c.data["command"] for c in calls] == ["timer_cancel", "power"]
