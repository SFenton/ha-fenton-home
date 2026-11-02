"""The control map (spec sections 4, 5 and 7): which Home Assistant action each button runs and what it waits for.

The map lives here, never on the phone. Every service call and every expected state is data built from
``entities.py``, so adjusting a mapping is a change to this file and never to the engine. ``expect`` is what the
engine waits up to 15 s for before it ends the card (Stop, Dock, Done), updates it (Pause, Resume) or reports that the
press did not work.
"""

from __future__ import annotations

from .entities import (
    COORDINATOR_CANCEL_REASON,
    COORDINATOR_CANCEL_SERVICE,
    COORDINATOR_DOMAIN,
    DISHWASHER,
    FAN_STOP_CANCELS_TIMER_FIRST,
    SIDES,
    Robot,
    fan_entities,
)
from .model import Control, Expect, ServiceCall

#: Button ids a problem row may ask for (``problems.TABLE``) mapped to their titles.
PROBLEM_BUTTON_TITLES = {"resume": "Resume", "dock": "Dock", "try_again": "Try Again", "vacuum_only": "Vacuum Only"}

DONE_TITLE = "Done"


def fan_stop(room: str) -> Control:
    """Stop turns the fan off (``power off``); it does not only cancel the timer (owner decision 4)."""
    entities = fan_entities(room)
    calls = [ServiceCall("script", entities["command"], {"command": "power", "target_power": "off"})]
    if FAN_STOP_CANCELS_TIMER_FIRST:
        calls.insert(0, ServiceCall("script", entities["command"], {"command": "timer_cancel"}))
    return Control(id="stop", title="Stop", verb="stop", calls=tuple(calls),
                   expect=(Expect(entities["pending"], frozenset({"off"})),))


def dishwasher_stop() -> Control:
    return Control(
        id="stop", title="Stop", verb="stop",
        calls=(ServiceCall("button", "press", {"entity_id": DISHWASHER["stop"]}),),
        expect=(Expect(DISHWASHER["state"], frozenset({"ready", "inactive", "aborting"})),))


def dishwasher_resume() -> Control:
    return Control(
        id="resume", title="Resume", verb="resume", outcome="update",
        calls=(ServiceCall("button", "press", {"entity_id": DISHWASHER["resume"]}),),
        expect=(Expect(DISHWASHER["state"], frozenset({"run"})),))


# -- Vacuums ----------------------------------------------------------------------------------------------------------
#
# ``session`` is whether the coordinator is running an automatic cleaning session (its ``auto_cleaning`` flag is on).


def vacuum_pause(robot: Robot, session: bool = False) -> Control:
    return Control(id="pause", title="Pause", verb="pause", outcome="update",
                   calls=(ServiceCall("vacuum", "pause", {"entity_id": robot.vacuum}),),
                   expect=(Expect(robot.vacuum, frozenset({"paused"})),))


def vacuum_resume(robot: Robot, session: bool = False) -> Control:
    return Control(id="resume", title="Resume", verb="resume", outcome="update",
                   calls=(ServiceCall("vacuum", "start", {"entity_id": robot.vacuum}),),
                   expect=(Expect(robot.vacuum, frozenset({"cleaning"})),))


def vacuum_dock(robot: Robot, session: bool = False) -> Control:
    """Dock: cancel the coordinator's session when it runs one (it sends the robot home), else go to the dock."""
    if session and robot.coordinator:
        call = ServiceCall(COORDINATOR_DOMAIN, COORDINATOR_CANCEL_SERVICE, {"reason": COORDINATOR_CANCEL_REASON})
    else:
        call = ServiceCall("vacuum", "return_to_base", {"entity_id": robot.vacuum})
    return Control(id="dock", title="Dock", verb="dock", calls=(call,),
                   expect=(Expect(robot.vacuum, frozenset({"returning", "docked"})),))


def vacuum_try_again(robot: Robot, session: bool = False) -> Control:
    """Can't-get-back-to-the-dock: try the return again."""
    return Control(id="try_again", title="Try Again", verb="try again",
                   calls=(ServiceCall("vacuum", "return_to_base", {"entity_id": robot.vacuum}),),
                   expect=(Expect(robot.vacuum, frozenset({"returning", "docked"})),))


def vacuum_only(robot: Robot, session: bool = False) -> Control:
    """Mop pads missing: switch the mode to vacuum, then resume."""
    return Control(id="vacuum_only", title="Vacuum Only", verb="resume", outcome="update",
                   calls=(ServiceCall("select", "select_option", {"entity_id": robot.mode_select, "option": "vacuum"}),
                          ServiceCall("vacuum", "start", {"entity_id": robot.vacuum})),
                   expect=(Expect(robot.vacuum, frozenset({"cleaning"})),))


_PROBLEM_BUILDERS = {
    "resume": vacuum_resume,
    "dock": vacuum_dock,
    "try_again": vacuum_try_again,
    "vacuum_only": vacuum_only,
}


def problem_button(button: str, robot: Robot | None, session: bool) -> Control | None:
    """A problem row's button (``resume``, ``dock``…) for a robot; None for a device that has no such control."""
    builder = _PROBLEM_BUILDERS.get(button)
    if builder is None or robot is None:
        return None
    return builder(robot, session)


def done(task_id: int, completed_by: int | None = None) -> Control:
    """Complete the linked Donetick task; the card ends on every device once the todo list shows it completed."""
    data: dict[str, object] = {"task_id": task_id}
    if completed_by is not None:
        data["completed_by"] = completed_by
    return Control(id="done", title=DONE_TITLE, verb="complete", confirm="task",
                   calls=(ServiceCall("donetick", "complete_task", data),))


# -- Hot flash --------------------------------------------------------------------------------------------------------


def hot_flash_stop(side_keys: tuple[str, ...]) -> Control:
    """Stop presses each in-view side's cancel button (both for Whole bed)."""
    sides = [SIDES[key] for key in side_keys]
    return Control(
        id="stop", title="Stop", verb="stop",
        calls=tuple(ServiceCall("input_button", "press", {"entity_id": side.cancel}) for side in sides),
        expect=tuple(Expect(side.phase, frozenset({"cooling", "holding"}), invert=True) for side in sides))
