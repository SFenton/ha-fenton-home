"""Content state v2, per-activity timestamps and the start sound (spec sections 3, 7.5 and 9)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fenton_home.live import ended_state_v2, state_v2
from fenton_home.payload import MAX_PAYLOAD_BYTES, PayloadError, Timestamps, build

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live2_samples  # noqa: E402
import samples  # noqa: E402

NOW = samples.FIXTURE_NOW

BLOCK = {"v": 2, "phase": "running", "headline": "Vacuuming Kitchen",
         "value": {"kind": "countup", "start_ts": NOW - 600},
         "progress": 1.0,
         "controls": [{"id": "pause", "title": "Pause"}, {"id": "dock", "title": "Dock", "role": "secondary"}],
         "url": "/sfenton-react-dash/home?path=vacuums", "stale_ts": NOW + 1800}

PROBLEM = {"severity": "critical", "code": "navigation.stuck", "icon": "alert", "problem": "Stuck in Kitchen",
           "message": "Move it somewhere flat, then tap Resume.", "chips": ["Since 2:58 PM"], "task_id": 4217}


def body(block: dict, event: str = "update", **data) -> dict:
    return {"message": "Main Floor Vacuum", "title": "Main Floor Vacuum", "live_activity_token": "b" * 64,
            "data": {"tag": "job.vacuum.main_floor", "live_update": True, "event": event, "fenton_activity": block, **data},
            "push_token": "a" * 64, "registration_info": samples.REGISTRATION_INFO}


def live(block: dict, event: str = "update", **data):
    return build(body(block, event, **data), critical_enabled=False, now=NOW)


def test_a_v2_block_becomes_the_content_state() -> None:
    state = live(BLOCK).payload["aps"]["content-state"]
    assert state == {"v": 2, "phase": "running", "headline": "Vacuuming Kitchen",
                     "value": {"kind": "countup", "start_ts": NOW - 600}, "progress": 1.0,
                     "controls": [{"id": "pause", "title": "Pause"}, {"id": "dock", "title": "Dock", "role": "secondary"}],
                     "url": "/sfenton-react-dash/home?path=vacuums"}
    aps = live(BLOCK).payload["aps"]
    assert aps["stale-date"] == NOW + 1800 and "stale_ts" not in json.dumps(state)


def test_the_start_carries_kind_symbol_and_tint_in_the_attributes() -> None:
    request = live(BLOCK, "start", fenton_attributes={"kind": "vacuum", "symbol": "vacuum", "tint": "cleaning",
                                                      "evil": "x"})
    aps = request.payload["aps"]
    assert aps["attributes"] == {"tag": "job.vacuum.main_floor", "title": "Main Floor Vacuum", "kind": "vacuum",
                                 "symbol": "vacuum", "tint": "cleaning"}
    assert aps["alert"] == {"title": "Main Floor Vacuum", "body": "Main Floor Vacuum"} and "sound" not in aps
    assert request.payload["fenton"] == {"v": 2, "tag": "job.vacuum.main_floor"} and request.priority == 10
    # Without them the app uses its defaults; an update never repeats the static attributes.
    assert live(BLOCK, "start").payload["aps"]["attributes"]["kind"] == "generic"
    assert "attributes" not in live(BLOCK, "update", fenton_attributes={"kind": "vacuum"}).payload["aps"]


def test_attribute_titles_are_capped_at_32_characters() -> None:
    long = body(BLOCK, "start")
    long["title"] = "T" * 60
    assert len(build(long, critical_enabled=False, now=NOW).payload["aps"]["attributes"]["title"]) == 32


def test_live_alert_on_a_start_adds_the_default_sound() -> None:
    request = live(BLOCK, "start", live_alert=True)
    assert request.payload["aps"]["sound"] == "default" and request.priority == 10
    update = live(BLOCK, "update", live_alert=True)
    assert update.payload["aps"]["sound"] == "default" and update.payload["aps"]["alert"]["title"] == "Main Floor Vacuum"
    assert "sound" not in live(BLOCK, "update").payload["aps"] and live(BLOCK, "update").priority == 5


def test_a_problem_card_may_carry_three_controls_and_other_cards_two() -> None:
    controls = [{"id": "resume", "title": "Resume"}, {"id": "dock", "title": "Dock"}, {"id": "done", "title": "Done"}]
    problem = {**BLOCK, "phase": "problem", "value": {"kind": "text", "text": "Needs help"}, "controls": controls,
               "problem": PROBLEM}
    state = live(problem).payload["aps"]["content-state"]
    assert [c["id"] for c in state["controls"]] == ["resume", "dock", "done"]
    assert state["problem"] == PROBLEM
    with pytest.raises(PayloadError):
        live({**BLOCK, "controls": controls})
    with pytest.raises(PayloadError):
        live({**problem, "controls": controls + [{"id": "x", "title": "X"}]})


def test_roles_and_the_first_control() -> None:
    state, _ = state_v2({**BLOCK, "controls": [{"id": "a", "title": "A", "role": "secondary"},
                                               {"id": "b", "title": "B", "role": "secondary"}]}, [])
    assert state["controls"] == [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "role": "secondary"}]


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(phase="exploding"),
    lambda b: b.pop("phase"),
    lambda b: b.update(value={"kind": "countdown"}),
    lambda b: b.update(value={"kind": "countup"}),
    lambda b: b.update(value={"kind": "percent"}) or b.pop("progress"),
    lambda b: b.update(value={"kind": "text"}),
    lambda b: b.update(value={"kind": "stopwatch"}),
    lambda b: b.update(value="12:00"),
    lambda b: b.update(controls="stop"),
    lambda b: b.update(controls=[{"id": "x"}]),
    lambda b: b.update(problem=PROBLEM),
    lambda b: b.update(phase="problem", problem=None),
    lambda b: b.update(phase="problem", problem={**PROBLEM, "severity": "dire"}),
    lambda b: b.update(phase="problem", problem={**PROBLEM, "message": ""}),
])
def test_structurally_wrong_blocks_are_refused(mutate) -> None:
    block = copy.deepcopy(BLOCK)
    mutate(block)
    with pytest.raises(PayloadError) as err:
        live(block)
    assert err.value.code == "invalid_activity"


def test_value_kinds_and_their_required_fields() -> None:
    countdown = live({**BLOCK, "value": {"kind": "countdown", "end_ts": NOW + 60, "start_ts": NOW - 60}})
    assert countdown.payload["aps"]["content-state"]["value"] == {"kind": "countdown", "end_ts": NOW + 60, "start_ts": NOW - 60}
    assert countdown.payload["aps"]["stale-date"] == NOW + 1800, "an explicit stale_ts wins"
    no_stale = {k: v for k, v in BLOCK.items() if k != "stale_ts"}
    assert live({**no_stale, "value": {"kind": "countdown", "end_ts": NOW + 60}}).payload["aps"]["stale-date"] == NOW + 660
    assert live(no_stale).payload["aps"]["stale-date"] == NOW + 1800
    paused = live({**BLOCK, "phase": "paused", "value": {"kind": "countup", "start_ts": NOW - 9, "paused_elapsed_s": 5.7}})
    assert paused.payload["aps"]["content-state"]["value"] == {"kind": "countup", "start_ts": NOW - 9, "paused_elapsed_s": 5}
    percent = live({**BLOCK, "value": {"kind": "percent"}, "progress": 0.456789})
    assert percent.payload["aps"]["content-state"]["progress"] == 0.457
    done = live({**BLOCK, "phase": "done", "value": {"kind": "text", "text": "x" * 40}, "controls": []})
    assert done.payload["aps"]["content-state"]["value"]["text"] == "x" * 15 + "…"
    estimated = live({**BLOCK, "value": {"kind": "countdown", "end_ts": NOW + 5, "estimated": True}})
    assert estimated.payload["aps"]["content-state"]["value"]["estimated"] is True
    # A countdown's start must precede its end, or it is dropped.
    odd = live({**BLOCK, "value": {"kind": "countdown", "end_ts": NOW + 5, "start_ts": NOW + 9}})
    assert "start_ts" not in odd.payload["aps"]["content-state"]["value"]


def test_unknown_keys_pending_and_icons_are_cleaned_with_notes() -> None:
    problem = {**BLOCK, "phase": "problem", "value": {"kind": "text", "text": "Needs help"},
               "controls": [], "problem": {**PROBLEM, "icon": "sparkles", "chips": ["a" * 40, "second"]},
               "pending": {"control": "stop", "label": "Stopping…", "since_ts": NOW}, "mystery": 1}
    request = live(problem)
    state = request.payload["aps"]["content-state"]
    assert "pending" not in state and "mystery" not in state
    assert state["problem"]["icon"] == "question" and state["problem"]["chips"] == ["a" * 19 + "…"]
    assert any("pending" in n for n in request.notes) and any("mystery" in n for n in request.notes)
    assert any("icon" in n for n in request.notes)


def test_detail_is_a_content_state_key() -> None:
    state, _ = state_v2({**BLOCK, "headline": None, "detail": "Stephen's side · by Home"}, [])
    assert state["detail"] == "Stephen's side · by Home" and "headline" not in state
    assert "detail" not in state_v2({**BLOCK, "detail": "  "}, [])[0]
    assert len(state_v2({**BLOCK, "detail": "d" * 50}, [])[0]["detail"]) == 32


def test_short_is_a_content_state_key_and_value_is_optional() -> None:
    block = {k: v for k, v in BLOCK.items() if k not in ("value", "progress")}
    state, _ = state_v2({**block, "short": "Kitchen"}, [])
    assert state["short"] == "Kitchen" and "value" not in state and "progress" not in state
    assert "short" not in state_v2({**block, "short": "  "}, [])[0] and "short" not in state_v2(block, [])[0]
    assert state_v2({**block, "short": "Master Bathroom"}, [])[0]["short"] == "Master Ba…"
    request = live({**block, "stale_ts": None})
    assert request.payload["aps"]["stale-date"] == NOW + 1800, "no value, no countdown: half an hour"
    # A value that is there is still validated; a percent still needs its progress.
    with pytest.raises(PayloadError):
        live({**block, "value": {"kind": "percent"}})


def test_strings_are_capped() -> None:
    state, _ = state_v2({**BLOCK, "headline": "h" * 50, "notice": "n" * 90,
                         "controls": [{"id": "i" * 60, "title": "t" * 60}]}, [])
    assert len(state["headline"]) == 32 and len(state["notice"]) == 60
    assert len(state["controls"][0]["title"]) == 20 and len(state["controls"][0]["id"]) == 40
    problem, _ = state_v2({**BLOCK, "phase": "problem", "controls": [],
                           "problem": {**PROBLEM, "problem": "p" * 80, "message": "m" * 200}}, [])
    assert len(problem["problem"]["problem"]) == 40 and len(problem["problem"]["message"]) == 90


def test_the_url_rules_match_v1() -> None:
    state, _ = state_v2({**BLOCK, "url": "/x?token=abc"}, [])
    assert "url" not in state
    state, _ = state_v2({**BLOCK, "url": "/" + "u" * 300}, [])
    assert "url" not in state


def test_the_worst_case_problem_card_fits_in_four_kilobytes() -> None:
    problem = {**BLOCK, "phase": "problem", "value": {"kind": "text", "text": "x" * 16}, "headline": "h" * 32,
               "controls": [{"id": "r" * 40, "title": "t" * 20}] * 3, "notice": "n" * 60,
               "problem": {**PROBLEM, "code": "c" * 80, "problem": "p" * 40, "message": "m" * 90, "chips": ["c" * 20]},
               "url": "/" + "u" * 190}
    start = body(problem, "start", live_alert=True, fenton_attributes={"kind": "k" * 30, "symbol": "s" * 30, "tint": "t" * 30})
    start["title"], start["message"] = "T" * 32, "M" * 400
    request = build(start, critical_enabled=False, now=NOW)
    assert len(request.body()) < MAX_PAYLOAD_BYTES
    start["message"] = "M" * 5000
    with pytest.raises(PayloadError) as err:
        build(start, critical_enabled=False, now=NOW)
    assert err.value.code == "payload_too_large"


def test_a_v1_block_still_goes_through_the_v1_path() -> None:
    request = live(samples.HOME_ACTIVITY, "update")
    assert request.payload["aps"]["content-state"]["v"] == 1
    assert request.payload["aps"]["content-state"]["jobs"][0]["id"] == "dishwasher"
    start = live(samples.HOME_ACTIVITY, "start", fenton_attributes={"kind": "vacuum"})
    assert start.payload["aps"]["attributes"] == {"tag": "job.vacuum.main_floor", "title": "Main Floor Vacuum", "kind": "home"}


def test_clear_notification_ends_a_job_with_a_v2_stub_and_the_home_activity_with_an_empty_list() -> None:
    end = build({"message": "clear_notification", "live_activity_token": "c" * 64,
                 "data": {"tag": "job.fan.guest", "event": "end"}}, critical_enabled=False, now=NOW)
    assert end.payload["aps"]["content-state"] == ended_state_v2() == {
        "v": 2, "phase": "done", "value": {"kind": "text", "text": "Ended"}, "controls": []}
    assert end.payload["aps"]["dismissal-date"] == NOW and end.priority == 10
    assert build(samples.LIVE_END, critical_enabled=False, now=NOW).payload["aps"]["content-state"] == {"v": 1, "jobs": []}


def test_timestamps_are_strictly_increasing_per_device_and_tag() -> None:
    stamps = Timestamps()
    assert stamps.next("hook:a", 100.2) == 101
    assert stamps.next("hook:a", 100.9) == 102, "never a tie, even within the same second"
    assert stamps.next("hook:a", 100.1) == 103
    assert stamps.next("hook:a", 200.0) == 200 and stamps.next("hook:a", 200.0) == 201
    assert stamps.next("hook:b", 100.2) == 101, "another activity has its own sequence"
    assert stamps.next("other:a", 50) == 50


def test_the_relay_stamps_each_push_newer_than_the_last() -> None:
    stamps = Timestamps()
    first = build(body(BLOCK), critical_enabled=False, now=NOW + 0.4, timestamps=stamps, ts_key="hook:t")
    second = build(body(BLOCK), critical_enabled=False, now=NOW + 0.5, timestamps=stamps, ts_key="hook:t")
    assert first.payload["aps"]["timestamp"] == NOW + 1 and second.payload["aps"]["timestamp"] == NOW + 2
    # The app's optimistic write is floor(now); a confirmation at the same second is still newer.
    assert second.payload["aps"]["timestamp"] > int(NOW + 0.5)


def test_the_live2_fixtures_are_what_the_engine_produces_and_the_relay_accepts() -> None:
    cases = live2_samples.cases()
    assert len(cases) >= 22
    for name, case in cases.items():
        request = build(case, critical_enabled=False, now=NOW)
        assert request.push_type == "liveactivity" and len(request.body()) < MAX_PAYLOAD_BYTES, name
        aps = request.payload["aps"]
        if aps["event"] == "start":
            assert aps["attributes"]["tag"].startswith("job.") and aps["attributes"]["kind"], name
        if name.startswith("live2-problem-critical"):
            assert aps["sound"] == "default" and aps["relevance-score"] == 100, name
        if name.startswith("live2-complete"):
            assert aps["content-state"]["value"]["text"] == "Complete" and aps["relevance-score"] == 10, name
