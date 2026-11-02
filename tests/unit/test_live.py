"""Live Activity payloads (contract v2, section 6)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fenton_home.live import (
    ATTRIBUTES_TYPE,
    MAX_JOBS,
    default_stale,
    empty_state,
    generic_state,
    home_state,
)
from fenton_home.payload import MAX_PAYLOAD_BYTES, PayloadError, build

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import samples  # noqa: E402

NOW = samples.FIXTURE_NOW


def live(body: dict, **kwargs):
    return build(body, critical_enabled=False, now=NOW, **kwargs)


def test_start_carries_attributes_a_quiet_alert_and_the_state() -> None:
    request = live(samples.LIVE_START)
    aps = request.payload["aps"]
    assert request.push_type == "liveactivity" and request.token_key == "live_activity_token"
    assert request.headers("com.sfenton.home")["apns-topic"] == "com.sfenton.home.push-type.liveactivity"
    assert request.headers("com.sfenton.home")["apns-priority"] == "10"
    assert aps["event"] == "start" and aps["timestamp"] == NOW
    assert aps["attributes-type"] == ATTRIBUTES_TYPE == "HomeActivityAttributes"
    assert aps["attributes"] == {"tag": "fenton-home", "title": "Home", "kind": "home"}
    assert aps["alert"] == {"title": "Home", "body": "Dishwasher started"} and "sound" not in aps
    assert aps["input-push-token"] == 1
    assert request.payload["fenton"] == {"v": 2, "tag": "fenton-home"}


def test_state_keeps_numbers_as_unix_seconds_and_strips_home_assistants_own_keys() -> None:
    state = live(samples.LIVE_START).payload["aps"]["content-state"]
    assert state["v"] == 1 and state["focus"] == "dishwasher" and state["url"] == "/sfenton-react-dash/home?path=kitchen"
    dishwasher, fan = state["jobs"]
    assert dishwasher["start_ts"] == NOW - 8_000 and dishwasher["end_ts"] == NOW + 2_520
    assert all(isinstance(dishwasher[key], int) for key in ("start_ts", "end_ts"))
    assert dishwasher["progress"] == 0.62 and dishwasher["tint"] == "cleaning" and dishwasher["symbol"] == "dishwasher"
    assert dishwasher["controls"] == [{"id": "stop", "title": "Stop", "role": "destructive"}]
    assert fan["controls"] == [{"id": "cancel_timer", "title": "Cancel Timer"}]
    assert "estimated" not in fan and "progress" not in fan
    assert "button.dishwasher_stop_program" not in json.dumps(state)


def test_update_is_quiet_and_goes_at_priority_five_with_a_stale_date() -> None:
    request = live(samples.LIVE_UPDATE)
    aps = request.payload["aps"]
    assert aps["event"] == "update" and "alert" not in aps and "attributes-type" not in aps
    assert request.priority == 5
    # The earliest end (the fan, 1,500 s away) plus ten minutes.
    assert aps["stale-date"] == NOW + 1_500 + 600
    assert "dismissal-date" not in aps


def test_an_alerting_or_urgent_update_goes_at_priority_ten() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["live_alert"] = True
    request = live(body)
    assert request.priority == 10
    assert request.payload["aps"]["alert"] == {"title": "Home", "body": body["message"]}
    assert request.payload["aps"]["sound"] == "default"
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["urgent"] = True
    assert live(body).priority == 10
    assert "alert" not in live(body).payload["aps"]


def test_an_explicit_stale_ts_wins_unless_it_is_in_the_past() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["fenton_activity"]["stale_ts"] = NOW + 5_000
    assert live(body).payload["aps"]["stale-date"] == NOW + 5_000
    body["data"]["fenton_activity"]["stale_ts"] = NOW - 5
    assert live(body).payload["aps"]["stale-date"] == NOW + 1_500 + 600
    assert "stale_ts" not in json.dumps(live(body).payload["aps"]["content-state"])


def test_end_on_clear_notification_removes_the_activity_at_once() -> None:
    request = live(samples.LIVE_END)
    aps = request.payload["aps"]
    assert aps["event"] == "end" and aps["content-state"] == empty_state()
    assert aps["dismissal-date"] == NOW and "stale-date" not in aps
    assert request.priority == 10 and "alert" not in aps


def test_end_with_a_final_state_lingers_for_dismiss_after() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"].update(event="end", dismiss_after=900)
    body["data"]["fenton_activity"]["jobs"][0].update(phase="done", status="Clean")
    aps = live(body).payload["aps"]
    assert aps["dismissal-date"] == NOW + 900
    assert aps["content-state"]["jobs"][0]["phase"] == "done"
    for value, expected, note in ((10 ** 6, 4 * 3600, "capped"), (-5, 0, "invalid"), ("soon", 0, "invalid"), (True, 0, "invalid")):
        body["data"]["dismiss_after"] = value
        request = live(body)
        assert request.payload["aps"]["dismissal-date"] == NOW + expected
        assert any(note in n for n in request.notes), value


def test_a_companion_app_notification_becomes_one_generic_job() -> None:
    request = live(samples.LIVE_GENERIC)
    aps = request.payload["aps"]
    assert aps["attributes"] == {"tag": "sprinklers-front", "title": "Watering", "kind": "generic"}
    assert aps["alert"] == {"title": "Watering", "body": "Sprinklers · Front lawn"}
    (job,) = aps["content-state"]["jobs"]
    assert job == {"id": "sprinklers-front", "kind": "generic", "title": "Watering", "status": "Sprinklers · Front lawn",
                   "phase": "running", "symbol": "drop.fill", "tint": "neutral", "start_ts": NOW, "end_ts": NOW + 600,
                   "progress": 0.25}
    assert aps["content-state"]["url"] == "/sfenton-react-dash/home?path=sprinklers"
    assert aps["stale-date"] == NOW + 600 + 600


def test_a_generic_end_reads_done() -> None:
    body = copy.deepcopy(samples.LIVE_GENERIC)
    body["data"]["event"] = "end"
    (job,) = live(body).payload["aps"]["content-state"]["jobs"]
    assert job["phase"] == "done" and "end_ts" not in job


@pytest.mark.parametrize(("data", "has_timer"), [
    ({"chronometer": True, "when": NOW + 300}, True),            # an absolute time in seconds
    ({"chronometer": True, "when": (NOW + 300) * 1000}, True),   # milliseconds
    ({"chronometer": True, "when": NOW - 300}, False),           # already past: a count-up is not drawn
    ({"chronometer": False, "when": NOW + 300}, False),
    ({"chronometer": True}, False),
    ({"chronometer": True, "when": "soon"}, False),
])
def test_generic_chronometer(data, has_timer) -> None:
    state = generic_state("t", "T", "m", data, NOW, [])
    assert ("end_ts" in state["jobs"][0]) is has_timer
    if has_timer:
        assert state["jobs"][0]["end_ts"] == NOW + 300 and state["jobs"][0]["start_ts"] == NOW


def test_generic_progress_and_url_rules() -> None:
    assert generic_state("t", "", "m", {"progress": 5, "progress_max": 4}, NOW, [])["jobs"][0]["progress"] == 1.0
    assert "progress" not in generic_state("t", "", "m", {"progress": 1, "progress_max": 0}, NOW, [])["jobs"][0]
    assert generic_state("t", "", "m", {}, NOW, [])["jobs"][0]["title"] == "Home"
    assert "url" not in generic_state("t", "", "m", {"url": "/x?token=abc"}, NOW, [])
    assert "url" not in generic_state("t", "", "m", {"url": "  "}, NOW, [])
    assert generic_state("t", "", "m", {"notification_icon": "mdi:unknown"}, NOW, [])["jobs"][0]["symbol"] == "circle"


def test_home_state_budget_and_cleaning() -> None:
    notes: list[str] = []
    jobs = [{"id": f"j{i}", "title": f"Job {i}", "status": "x" * 80,
             "controls": [{"id": f"c{n}", "title": "C"} for n in range(4)]} for i in range(9)]
    state, stale = home_state({"jobs": jobs, "focus": "j8", "stale_ts": NOW + 10}, notes)
    assert stale == NOW + 10 and len(state["jobs"]) == MAX_JOBS
    assert state["focus"] == "j8" and state["jobs"][-1]["id"] == "j8", "the focused job survives the cut"
    assert all(len(j["status"]) == 40 and j["status"].endswith("…") and len(j["controls"]) == 2 for j in state["jobs"])
    assert any("more than 6 jobs" in n for n in notes) and any("2 controls" in n for n in notes)


def test_home_state_drops_what_the_app_cannot_read() -> None:
    notes: list[str] = []
    state, _ = home_state({
        "focus": "gone", "url": "/x?access_token=abc",
        "jobs": [
            {"id": "a", "title": "A", "phase": "sprinting", "progress": 7, "estimated": True, "start_ts": True, "end_ts": -1,
             "controls": [{"id": "ok", "title": "OK", "role": "shiny"}, {"title": "no id"}, "junk"]},
            {"title": "no id"}, "junk", {"id": "x"},
        ],
    }, notes)
    assert "focus" not in state and "url" not in state
    (job,) = state["jobs"]
    assert job["phase"] == "running" and job["progress"] == 1.0 and job["estimated"] is True
    assert "start_ts" not in job and "end_ts" not in job
    assert job["controls"] == [{"id": "ok", "title": "OK"}]
    assert any("unknown phase" in n for n in notes) and any("url dropped" in n for n in notes)
    state, _ = home_state({"jobs": [], "url": "x" * 300}, [])
    assert "url" not in state


@pytest.mark.parametrize("block", ["x", [], {}, {"jobs": "x"}])
def test_a_malformed_block_is_refused(block) -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["fenton_activity"] = block
    with pytest.raises(PayloadError) as err:
        live(body)
    assert err.value.code == "invalid_activity"


def test_invalid_events_and_missing_tags() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["event"] = "explode"
    with pytest.raises(PayloadError) as err:
        live(body)
    assert err.value.code == "invalid_event"
    body["data"].pop("tag")
    with pytest.raises(PayloadError) as err:
        live(body)
    assert err.value.code == "tag_required"


def test_an_oversized_state_is_refused() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["fenton_activity"]["jobs"] = [
        {"id": f"job-{i}", "title": "T" * 40, "status": "S" * 40, "kind": "k" * 40, "symbol": "s" * 40, "tint": "t" * 40,
         "controls": [{"id": "c" * 40, "title": "C" * 40}, {"id": "d" * 40, "title": "D" * 40}]} for i in range(6)]
    body["data"]["fenton_activity"]["url"] = "/" + "u" * 190
    body["message"] = "m" * 3000
    body["data"]["event"] = "start"
    with pytest.raises(PayloadError) as err:
        live(body)
    assert err.value.code == "payload_too_large"
    assert MAX_PAYLOAD_BYTES == 4096


def test_the_relevance_score_is_passed_when_numeric() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    body["data"]["relevance_score"] = "0.75"
    assert live(body).payload["aps"]["relevance-score"] == 0.75
    body["data"]["relevance_score"] = "high"
    request = live(body)
    assert "relevance-score" not in request.payload["aps"] and any("relevance_score" in n for n in request.notes)


def test_a_live_update_without_a_token_is_dropped_not_delivered_as_a_banner() -> None:
    body = copy.deepcopy(samples.LIVE_UPDATE)
    del body["live_activity_token"]
    request = live(body)
    assert request.dropped and request.payload == {} and request.notes
    # Anything else without a token is an ordinary notification.
    plain = {"message": "hi", "data": {"tag": "t"}}
    assert not live(plain).dropped
    # ``clear_notification`` without an activity token still clears delivered notifications.
    assert live(samples.CLEAR).push_type == "background"


def test_default_stale() -> None:
    state = {"jobs": [{"phase": "running", "end_ts": NOW + 100}, {"phase": "done", "end_ts": NOW - 9_999}]}
    assert default_stale(state, NOW) == NOW + 100 + 600
    assert default_stale({"jobs": [{"phase": "running"}]}, NOW) == NOW + 1800
    assert default_stale({"jobs": [{"phase": "running", "end_ts": NOW - 5}]}, NOW) == NOW + 600
