"""The problem table, its wording and the raw-text fallbacks (spec section 5)."""

from __future__ import annotations

from datetime import UTC

import pytest
from fenton_home import controls
from fenton_home.entities import ROBOTS, resolve_device
from fenton_home.live import PROBLEM_ICONS
from fenton_home.problems import (
    ICONS,
    ROWS,
    TABLE,
    build_problem,
    code_for_raw,
    kind_of,
    quote_message,
    slug,
)

ROBOT = ROBOTS["vacuum.main_floor"]
NOW = 1_792_249_200


def make(button: str):
    return controls.problem_button(button, ROBOT, True)


def build(code: str, raw: str | None = None, **kwargs):
    kwargs.setdefault("room", "Kitchen")
    kwargs.setdefault("since", NOW - 120)
    return build_problem(code, raw, tz=UTC, make_button=make, **kwargs)


# The wording from the spec table: code, severity, icon, problem, message, button titles.
SPEC = [
    ("navigation.stuck", "critical", "alert", "Stuck in Kitchen", "Move it somewhere flat, then tap Resume.",
     ["Resume", "Dock"]),
    ("navigation.target_unreachable", "critical", "door", "Can't reach Kitchen",
     "A door may be closed. Open it, then tap Resume.", ["Resume", "Dock"]),
    ("navigation.dock_unreachable", "critical", "dock", "Can't get back to the dock",
     "Clear the way to the dock or carry it there.", ["Try Again"]),
    ("navigation.ramp_hazard", "critical", "edge", "Stopped at a drop or ramp",
     "Move it away from the edge, then tap Resume.", ["Resume", "Dock"]),
    ("maintenance.main_brush_jammed", "critical", "brush", "Main brush jammed",
     "Turn it over, clear the brush, then tap Resume.", ["Resume", "Dock"]),
    ("maintenance.side_brush_jammed", "critical", "brush", "Side brush jammed",
     "Untangle the side brush, then tap Resume.", ["Resume", "Dock"]),
    ("power.critical_battery", "critical", "battery", "Battery critically low", "Carry it to the dock to charge.", []),
    ("power.low_battery", "warning", "battery", "Battery low",
     "Heading to the dock. It finishes Kitchen after charging.", []),
    ("mop.clean_water_empty", "warning", "water", "Clean water tank empty",
     "Refill the clean water tank on the dock, then tap Resume.", ["Resume"]),
    ("mop.dirty_water_unavailable", "warning", "waste", "Wastewater tank full or missing",
     "Empty the wastewater tank and seat it firmly.", ["Resume"]),
    ("mop.attachment_missing", "warning", "mop", "Mop pads missing", "Attach the mop pads, or it vacuums only.",
     ["Vacuum Only"]),
    ("mop.vacuum_only_fallback", "warning", "mop", "Vacuuming only",
     "Mopping isn't available. Check the mop attachment, water tanks and pads.", []),
    ("dock.cover_open", "warning", "bag", "Dock cover open", "Check the dust bag is in, then close the cover.", []),
    ("dock.dustbag_blocked", "warning", "bag", "Dust bag full", "Replace the dust bag in the dock.", []),
    ("dishwasher.action_required", "warning", "door_dish", "Needs attention", "Check the dishwasher's display.", []),
    ("dishwasher.error", "critical", "question", "Stopped with an error", "Check the dishwasher's display.", []),
]


@pytest.mark.parametrize(("code", "severity", "icon", "problem", "message", "buttons"), SPEC)
def test_the_table_matches_the_spec(code, severity, icon, problem, message, buttons) -> None:
    info = build(code)
    assert (info.severity, info.icon, info.problem, info.message) == (severity, icon, problem, message)
    assert [c.title for c in info.controls] == buttons
    assert info.chips == ()
    assert info.task_id is None and "done" not in [c.id for c in info.controls]


def test_every_code_in_the_table_is_in_the_spec_list_and_every_icon_is_known() -> None:
    assert {row.code for row in ROWS} - {s[0] for s in SPEC} == {"dishwasher.rinse_aid"}
    assert {row.icon for row in ROWS} <= ICONS == PROBLEM_ICONS
    assert all(len(row.problem.format(room="Living Room")) <= 40 and len(row.message.format(room="Living Room")) <= 90
               for row in ROWS)


def test_a_room_that_is_unknown_has_its_own_wording() -> None:
    assert build("navigation.stuck", room=None).problem == "Stuck"
    assert build("navigation.target_unreachable", room=None).problem == "Can't reach its room"
    assert build("power.low_battery", room=None).message == "Heading to the dock. It finishes after charging."


def test_done_is_last_and_only_with_a_linked_task() -> None:
    info = build("navigation.stuck", task_id=4217, make_done=controls.done)
    assert [c.id for c in info.controls] == ["resume", "dock", "done"] and info.task_id == 4217
    done = info.controls[-1]
    assert done.calls[0].domain == "donetick" and done.calls[0].service == "complete_task"
    assert done.calls[0].data == {"task_id": 4217} and done.confirm == "task"
    assert controls.done(5, 9).calls[0].data == {"task_id": 5, "completed_by": 9}
    assert build("navigation.stuck", task_id=4217).controls[-1].id == "dock", "no done factory, no Done"


def test_unknown_numbered_errors_keep_only_the_error_chip() -> None:
    info = build("vendor.unknown_error_12", "Unknown error 12")
    assert (info.problem, info.message, info.severity, info.icon) == (
        "Stopped with an error", "Check the robot, then tap Resume.", "critical", "question")
    assert info.chips == ("Error 12",) and [c.title for c in info.controls] == ["Resume", "Dock"]


def test_unmapped_errors_quote_the_robot_and_have_no_chips() -> None:
    info = build("unmapped.wheel_tangled", "Wheel tangled")
    assert info.problem == "Stopped with an error"
    assert info.message == 'The robot says "Wheel tangled". Check it, then tap Resume.'
    assert info.chips == () and [c.title for c in info.controls] == ["Resume", "Dock"]


def test_quoted_text_is_capped_at_60_characters_and_the_sentence_at_90() -> None:
    long = "x" * 200
    message = quote_message(long)
    assert len(message) <= 90 and message.startswith('The robot says "') and "…" in message
    assert quote_message("  two   spaces ") == 'The robot says "two spaces". Check it, then tap Resume.'
    assert len(quote_message("y" * 60)) <= 90


def test_a_service_code_the_table_does_not_know_uses_the_automations_wording() -> None:
    info = build("custom.thing", None, title="Salt low", message="Add dishwasher salt.")
    assert (info.problem, info.message, info.controls) == ("Salt low", "Add dishwasher salt.", ())
    assert build("custom.thing", None).message == "Check the device, then tap Resume."
    assert build("custom.thing", "oops", title="Salt low").message == 'The robot says "oops". Check it, then tap Resume.'


def test_vacuum_only_switches_the_mode_then_starts() -> None:
    assert controls.problem_button("resume", None, False) is None
    assert controls.problem_button("bogus", ROBOT, False) is None
    for robot, mode in (("vacuum.main_floor", "valetudo_exaltedsneakydeer"), ("vacuum.music_room", "valetudo_elatedusedram"),
                        ("vacuum.theater_room", "valetudo_politefatherlykingfisher")):
        vacuum_only = controls.problem_button("vacuum_only", ROBOTS[robot], True)
        assert [(c.domain, c.service) for c in vacuum_only.calls] == [("select", "select_option"), ("vacuum", "start")]
        assert vacuum_only.calls[0].data == {"entity_id": f"select.{mode}_mode", "option": "vacuum"}


def test_problem_buttons_resume_with_vacuum_start_and_dock_by_session() -> None:
    for session in (True, False):
        assert controls.problem_button("resume", ROBOT, session).calls[0].service == "start"
    assert controls.problem_button("dock", ROBOT, False).calls[0].service == "return_to_base"
    cancel = controls.problem_button("dock", ROBOT, True).calls[0]
    assert (cancel.domain, cancel.service, dict(cancel.data)) == ("valetudo_vacuum_coordinator", "cancel_session",
                                                                 {"reason": "live_activity"})
    # Only the Main Floor robot has a coordinator; the others dock by themselves.
    assert controls.problem_button("dock", ROBOTS["vacuum.music_room"], True).calls[0].service == "return_to_base"
    assert controls.problem_button("try_again", ROBOT, True).calls[0].service == "return_to_base"


@pytest.mark.parametrize(("raw", "code"), [
    ("Unknown error 75", "power.critical_battery"),
    ("unknown error 95", "navigation.ramp_hazard"),
    ("Unknown Error 120", "mop.attachment_missing"),
    ("Unknown error 12", "vendor.unknown_error_12"),
    ("Main brush jammed", "maintenance.main_brush_jammed"),
    ("Side brush stuck", "maintenance.side_brush_jammed"),
    ("Dust bag missing", "dock.dustbag_blocked"),
    ("Low battery", "power.low_battery"),
    ("Robot is stuck", "navigation.stuck"),
    ("Wheel tangled in a cable", "unmapped.wheel_tangled_in_a_cable"),
    ("!!!", "unmapped.error"),
    ("No error", None), ("", None), (None, None), ("unavailable", None),
])
def test_code_for_raw(raw, code) -> None:
    assert code_for_raw(raw) == code


def test_kinds_and_slugs() -> None:
    assert kind_of("navigation.stuck") == "table" and kind_of("vendor.unknown_error_3") == "numbered"
    assert kind_of("unmapped.x") == "unmapped" and kind_of("whatever") == "unmapped"
    assert slug("Wheel  tangled!") == "wheel_tangled" and len(slug("a" * 100)) == 40
    assert set(TABLE) == {row.code for row in ROWS}


def test_devices_resolve_to_job_ids() -> None:
    for name, job in [("main_floor", "vacuum.main_floor"), ("Main Floor", "vacuum.main_floor"),
                      ("vacuum.music_room", "vacuum.music_room"), ("theater-room", "vacuum.theater_room"),
                      ("dishwasher", "dishwasher"), ("main_floor_vacuum", "vacuum.main_floor"),
                      ("job.vacuum.main_floor", "vacuum.main_floor"), ("guest", "fan.guest")]:
        assert resolve_device(name) == job, name
    assert resolve_device("garage") is None and resolve_device("") is None
