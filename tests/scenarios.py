"""Recorded Home Assistant states for the job engine's tests and the ``live2-*`` fixtures.

Each function returns a ``States`` snapshot built from the entity names in the Live Activities per-job spec, as they read
at ``NOW`` (2026-10-17 15:00:00 UTC). No Home Assistant is involved; the engine's pure modules are run against these.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fenton_home.engine import Engine, End, Push, Registration
from fenton_home.jobs import Ctx
from fenton_home.snapshot import States

NOW = 1_792_249_200
STEPHEN, STEPHANIE = "person.stephen_fenton", "person.stephanie_hobart"
USER_STEPHEN, USER_STEPHANIE = "user-stephen", "user-stephanie"

PEOPLE_STATES = {
    STEPHEN: ("home", {"user_id": USER_STEPHEN, "friendly_name": "Stephen Fenton"}),
    STEPHANIE: ("home", {"user_id": USER_STEPHANIE, "friendly_name": "Stephanie Hobart"}),
}

PHONE = Registration("hook-stephen-iphone", USER_STEPHEN, "Stephen's iPhone · Fenton", STEPHEN)
IPAD = Registration("hook-stephen-ipad", USER_STEPHEN, "Stephen's iPad · Fenton", STEPHEN)
STEPH_PHONE = Registration("hook-steph-iphone", USER_STEPHANIE, "Steph's iPhone · Fenton", STEPHANIE)


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def ctx(**kwargs: Any) -> Ctx:
    from fenton_home.model import PersonRef  # noqa: PLC0415

    people = (PersonRef(STEPHEN, USER_STEPHEN, "Stephen Fenton"), PersonRef(STEPHANIE, USER_STEPHANIE, "Stephanie Hobart"))
    return Ctx(tz=UTC, people=people, **kwargs)


def states(**spec: Any) -> States:
    """The household at rest, plus ``spec`` (``entity_id=...``, ``.`` written as ``__``)."""
    return States.build({**PEOPLE_STATES, "binary_sensor.effective_vacation_mode": "off",
                         "sensor.household_away_status": ("home", {"mode": "home"})}).with_(**spec)


# -- Fans ---------------------------------------------------------------------------------------------------------------


def fan(room: str = "guest", *, ends_in: float = 1500, started_ago: float = 300) -> States:
    return states(**{
        f"input_boolean__{room}_bathroom_fan_timer_pending": ("on", {}, NOW - started_ago),
        f"input_datetime__{room}_bathroom_fan_timer_ends_at": ("2026-10-17 15:25:00", {"timestamp": NOW + ends_in}),
    })


# -- Dishwasher ---------------------------------------------------------------------------------------------------------


def dishwasher(op: str = "run", *, finish_in: float | None = 2520, progress: float | None = None,
               remote: bool = True, start_in: float | None = None) -> States:
    spec: dict[str, Any] = {
        "sensor__dishwasher_operation_state": (op, {}, NOW - 8000),
        "binary_sensor__dishwasher_remote_control": "on" if remote else "off",
    }
    if start_in is not None:
        spec["number__dishwasher_start_in_relative"] = str(start_in)
    spec["sensor__dishwasher_program_finish_time"] = iso(NOW + finish_in) if finish_in is not None else "unavailable"
    if progress is not None:
        spec["sensor__dishwasher_program_progress"] = str(progress)
    return states(**spec)


# -- Laundry ------------------------------------------------------------------------------------------------------------


def laundry(job: str = "dryer", on: bool = True, *, on_for: float = 1800) -> States:
    return states(**{f"input_boolean__{job}_started_helper": ("on" if on else "off", {}, NOW - on_for)})


# -- Vacuums ------------------------------------------------------------------------------------------------------------


def main_floor(vac: str = "cleaning", *, room_id: str = "kitchen", phase: str = "cleaning", auto: bool = True,
               vacuum_only: bool = False, mode: str = "vacuum_and_mop", changed: float | None = None, **extra: Any) -> States:
    return states(**{
        "vacuum__valetudo_exaltedsneakydeer": (vac, {}, changed if changed is not None else NOW - 600),
        "sensor__main_floor_vacuum_coordinator_current_room": (
            room_id or "none", {"room_id": room_id, "phase": phase, "vacuum_only": vacuum_only,
                                "start_confirmed_at": iso(NOW - 900)}),
        "binary_sensor__main_floor_vacuum_coordinator_auto_cleaning": "on" if auto else "off",
        "sensor__main_floor_vacuum_coordinator_kitchen_last_cleaned": ("2026-10-16", {"room_name": "Kitchen"}),
        "select__valetudo_exaltedsneakydeer_mode": mode,
        **extra,
    })


def issue(code: str, raw: str | None = None, *, reported: float | None = None) -> dict[str, Any]:
    """A status sensor's ``current_issue`` while a problem is active."""
    return {"code": code, "raw": raw or code, "reported_at": iso(NOW - 120 if reported is None else reported),
            "provenance": "coordinator"}


CLEAR = {"status": "clear"}


def main_floor_issue(code: str, *, room_id: str = "kitchen", vac: str = "error", clear: bool = False) -> States:
    return main_floor(vac, room_id=room_id, phase="cleaning", **{
        "sensor__main_floor_vacuum_status": ("clear" if clear else "error", {"current_issue": CLEAR if clear else issue(code)})})


ROBOT_IDS = {"music_room": "elatedusedram", "theater_room": "politefatherlykingfisher", "main_floor": "exaltedsneakydeer"}


def manual_run(robot: str = "music_room", *, flag: str = "none", vac: str = "cleaning", segment: str | None = None) -> States:
    rid = ROBOT_IDS[robot]
    spec: dict[str, Any] = {
        f"vacuum__valetudo_{rid}": (vac, {}, NOW - 300),
        f"sensor__valetudo_{rid}_status_flag": flag,
    }
    if segment is not None:
        spec[SEGMENT_SENSORS[robot]] = (segment, {"segment_id": "16"})
    return states(**spec)


SEGMENT_SENSORS = {"music_room": "sensor__music_room_music_room_robot_vacuum_estimated_segment",
                   "theater_room": "sensor__theater_room_vacuum_estimated_segment_2",
                   "main_floor": "sensor__main_floor_robot_vacuum_estimated_segment"}


def robot_issue(code: str, raw: str | None = None, robot: str = "music_room") -> States:
    """The robot's status sensor reports an active issue and the robot has stopped."""
    return states(**{f"vacuum__valetudo_{ROBOT_IDS[robot]}": ("error", {}, NOW - 60),
                     f"sensor__{robot}_vacuum_status": ("error", {"current_issue": issue(code, raw)})})


def robot_error(raw: str, robot: str = "music_room") -> States:
    """No status sensor at all (it is missing): only the robot's own error text, mapped by keyword."""
    rid = ROBOT_IDS[robot]
    return states(**{f"vacuum__valetudo_{rid}": ("error", {}, NOW - 60),
                     f"sensor__valetudo_{rid}_error": (raw, {}, NOW - 60)})


#: The music-room robot's run of 2026-11-13, from the recorder: seconds after the vacuum entity read ``cleaning``, the
#: status flag, and the estimated segment (rooms renamed to ones this file already uses). The vacuum changed 8 ms before
#: its flag; the segment then left the room for 10 min, came back for 20 s, left again for 7 min 30 s and came back.
MUSIC_ROOM_RUN = (
    (0.000, "cleaning", "none", "Music Room"),
    (0.008, "cleaning", "segment", "Music Room"),
    (66.029, "cleaning", "segment", "Pantry"),
    (676.264, "cleaning", "segment", "Music Room"),
    (696.270, "cleaning", "segment", "Hall"),
    (1146.442, "cleaning", "segment", "Music Room"),
    (6811.245, "returning", "resumable", "Music Room"),
)


def music_room_run(step: int) -> States:
    """The snapshot after the ``step``-th change of ``MUSIC_ROOM_RUN``."""
    _, vac, flag, segment = MUSIC_ROOM_RUN[step]
    return manual_run("music_room", flag=flag, vac=vac, segment=segment)


# -- Hot flash ----------------------------------------------------------------------------------------------------------


def hot_flash(left: str = "idle", right: str = "idle", *, by_left: str = "stephen", by_right: str = "steph",
              left_ends_in: float = 800, right_ends_in: float = 800, away: tuple[str, dict] | None = None,
              vacation: bool = False, temp: float = 62.0) -> States:
    spec: dict[str, Any] = {
        "input_select__sleepypod_hot_flash_left_phase": left,
        "input_select__sleepypod_hot_flash_right_phase": right,
        "timer__sleepypod_hot_flash_left": ("active", {"finishes_at": iso(NOW + left_ends_in)}),
        "timer__sleepypod_hot_flash_right": ("active", {"finishes_at": iso(NOW + right_ends_in)}),
        "input_text__eight_sleep_stephen_hot_flash_requested_by": by_left,
        "input_text__eight_sleep_steph_hot_flash_requested_by": by_right,
        "climate__sleepypod_eight_pod_left_side": ("heat_cool", {"current_temperature": temp}),
        "climate__sleepypod_eight_pod_right_side": ("heat_cool", {"current_temperature": temp}),
        "binary_sensor__effective_vacation_mode": "on" if vacation else "off",
    }
    if away:
        spec["sensor__household_away_status"] = away
    return states(**spec)


# -- Running the engine -------------------------------------------------------------------------------------------------


def run(engine: Engine, snapshot: States, now: float = NOW, regs: list[Registration] | None = None, **ctx_kwargs: Any):
    """Refresh and plan once; returns the plan."""
    engine.refresh(snapshot, now, ctx(**ctx_kwargs))
    return engine.plan(regs if regs is not None else [PHONE], now)


def pushes(plan) -> list[Push]:
    return [a for a in plan.actions if isinstance(a, Push)]


def ends(plan) -> list[End]:
    return [a for a in plan.actions if isinstance(a, End)]


def after(seconds: float) -> float:
    return NOW + seconds


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)
