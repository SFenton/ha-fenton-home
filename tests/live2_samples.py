"""``live2-*.apns`` fixtures: the job engine's own output, run through the relay at a fixed time.

Each case is run through ``Engine`` on a recorded snapshot (``scenarios.py``), the resulting ``notify`` call is turned into
the body ``mobile_app`` would post to the relay, and ``tests/unit/test_payload.py`` maps it to the APNs payload. The app's
repository mirrors them in ``tools/push_fixtures/`` and decodes every one.
"""

from __future__ import annotations

from typing import Any

import scenarios as sc
from fenton_home.engine import End, Engine, Push, service_data

from samples import REGISTRATION_INFO

STEPH_VIEW = sc.STEPH_PHONE


def body_for(action: Push | End, event: str, token: str = "b" * 64) -> dict[str, Any]:
    """What ``mobile_app`` posts to the relay for a planned push."""
    data = service_data(action)
    body = {"message": data["message"], "live_activity_token": token,
            "data": {**data["data"], "event": event},
            "push_token": "a" * 64, "registration_info": REGISTRATION_INFO}
    if "title" in data:
        body["title"] = data["title"]
    return body


def _only(plan: Any, tag: str) -> Push:
    (push,) = [a for a in sc.pushes(plan) if a.tag == tag]
    return push


def first(snapshot: Any, tag: str, regs: list | None = None) -> Push:
    return _only(sc.run(Engine(), snapshot, regs=regs), tag)


def complete(before: Any, after_: Any, tag: str, regs: list | None = None) -> Push:
    """The Complete card: the job runs, then finishes."""
    engine = Engine()
    sc.run(engine, before, regs=regs)
    plan = sc.run(engine, after_, sc.NOW + 30, regs=regs)
    return _only(plan, tag)


def cases() -> dict[str, dict[str, Any]]:
    """Fixture name to the relay body."""
    out: dict[str, dict[str, Any]] = {}

    def add(name: str, push: Push, event: str = "start") -> None:
        out[f"live2-{name}"] = body_for(push, event)

    add("fan-start", first(sc.fan("guest"), "job.fan.guest"))
    engine = Engine()
    sc.run(engine, sc.fan("guest"))
    (stop,) = sc.ends(sc.run(engine, sc.states(), sc.NOW + 60))
    out["live2-fan-stop-end"] = body_for(stop, "end")
    add("dishwasher-countdown", first(sc.dishwasher(), "job.dishwasher"))
    add("dishwasher-percent", first(sc.dishwasher(finish_in=None, progress=62), "job.dishwasher"))
    add("washer-countup", first(sc.laundry("washer", on_for=420), "job.washer"))
    add("dryer-countup", first(sc.laundry("dryer"), "job.dryer"))
    add("complete-dishwasher", first(sc.dishwasher("finished"), "job.dishwasher"), "update")
    add("complete-washer", complete(sc.laundry("washer"), sc.laundry("washer", on=False, on_for=0), "job.washer"), "update")
    add("complete-dryer", complete(sc.laundry("dryer"), sc.laundry("dryer", on=False, on_for=0), "job.dryer"), "update")
    add("complete-hot-flash", first(sc.hot_flash("restore_pending", "idle", by_left="stephen"), "job.hot_flash"), "update")
    add("hot-flash-cooling", first(sc.hot_flash("cooling", "idle", temp=70), "job.hot_flash"))
    add("hot-flash-holding", first(sc.hot_flash("holding", "idle"), "job.hot_flash"), "update")
    add("hot-flash-by-steph", first(sc.hot_flash("holding", "idle", by_left="steph"), "job.hot_flash"))
    add("hot-flash-whole-bed", first(sc.hot_flash("holding", "holding", by_left="stephen", by_right="stephen",
                                                  left_ends_in=800, right_ends_in=860), "job.hot_flash"))
    add("vacuum-countup", first(sc.main_floor(), "job.vacuum.main_floor"))
    add("vacuum-paused", first(sc.main_floor("paused"), "job.vacuum.main_floor"), "update")
    add("problem-critical", first(sc.main_floor_issue("navigation.stuck"), "job.vacuum.main_floor"))
    add("problem-warning", first(sc.main_floor_issue("mop.clean_water_empty", vac="paused"), "job.vacuum.main_floor"))
    add("problem-unknown-error", first(sc.robot_issue("vendor.unknown_error_12", "Unknown error 12"), "job.vacuum.music_room"))
    add("problem-unmapped", first(sc.robot_issue("unmapped.wheel_tangled_in_a_cable", "Wheel tangled in a cable"), "job.vacuum.music_room"))
    add("problem-no-task", first(sc.main_floor_issue("power.low_battery", vac="returning"), "job.vacuum.main_floor"))
    # A problem with a linked Donetick task carries Done last, so three controls.
    engine = Engine()
    sc.run(engine, sc.states())
    engine.raise_problem("vacuum.main_floor", "navigation.stuck", None, sc.NOW, task_title="Main Floor Vacuum · Robot Stuck")
    engine.records["vacuum.main_floor"].task_id = 4217
    engine.records["vacuum.main_floor"].room = "Kitchen"
    engine.records["vacuum.main_floor"].since = sc.NOW - 120
    add("problem-with-done", _only(sc.run(engine, sc.main_floor_issue("navigation.stuck")), "job.vacuum.main_floor"))
    # The v1 Home activity still goes through the relay unchanged for one release.
    from samples import LIVE_UPDATE  # noqa: PLC0415

    out["live2-v1-compat"] = LIVE_UPDATE
    return out
