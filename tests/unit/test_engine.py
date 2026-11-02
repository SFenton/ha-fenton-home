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


def test_a_service_raised_problem_with_a_task_stays_until_the_task_is_completed() -> None:
    engine = Engine()
    engine.raise_problem("vacuum.main_floor", "navigation.stuck", None, NOW, task_title="Main Floor Vacuum · Robot Stuck",
                         push_tag="vacuum-kitchen-error")
    record = engine.records["vacuum.main_floor"]
    record.task_id = 4217
    stuck = sc.main_floor_issue("navigation.stuck")
    (push,) = sc.pushes(run(engine, stuck))
    assert [c["id"] for c in push.content["controls"]] == ["resume", "dock", "done"]
    assert push.content["problem"]["task_id"] == 4217
    # The robot recovers and is cleaning again; the task is still open, so the card stays a problem card.
    still = run(engine, sc.main_floor(), 60)
    assert all(p.content["phase"] == "problem" for p in sc.pushes(still))
    assert sc.ends(still) == []
    # The task is completed (by Home Assistant, a person or Done): the card ends everywhere.
    record.task_completed = True
    plan = run(engine, sc.main_floor(), 120)
    assert len(sc.pushes(plan)) == 1 and sc.pushes(plan)[0].content["phase"] == "running"
    assert engine.records == {}
    assert [r.push_tag for r in engine.take_ended_records()] == ["vacuum-kitchen-error"]


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
