"""Mapping mobile_app push bodies to APNs requests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fenton_home.payload import DYNAMIC_CATEGORY, MAX_PAYLOAD_BYTES, PayloadError, build, collapse_id

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live2_samples  # noqa: E402
import samples  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_front_door_lifecycle_maps_every_key() -> None:
    request = build(samples.FRONT_DOOR_UNSECURED, critical_enabled=False)
    aps, fenton = request.payload["aps"], request.payload["fenton"]
    assert request.push_type == "alert" and request.priority == 10
    assert request.collapse_id == "sfenton-front-door-unsecured-42"
    assert aps["alert"] == {"title": "Front Door · Unlocked While Away",
                            "body": samples.FRONT_DOOR_UNSECURED["message"]}
    assert aps["interruption-level"] == "time-sensitive" and aps["sound"] == "default"
    assert aps["thread-id"] == "front-door-unsecured"
    assert aps["category"] == DYNAMIC_CATEGORY and aps["mutable-content"] == 1
    assert fenton["tag"] == "sfenton-front-door-unsecured-42"
    assert fenton["url"] == "/sfenton-react-dash/home?path=security"
    assert fenton["actions"][0] == {"action": "FRONT_DOOR_LOCK_42", "title": "Confirm: Lock Front Door",
                                    "authenticationRequired": True, "activationMode": "background",
                                    "icon": "sfsymbols:lock"}
    # alert_once and clickAction are Android/companion-only and don't leak into extra.
    assert "extra" not in fenton


def test_critical_is_downgraded_until_enabled() -> None:
    off = build(samples.DOORBELL, critical_enabled=False)
    assert off.payload["aps"]["interruption-level"] == "time-sensitive"
    assert off.payload["aps"]["sound"] == "default"
    assert off.notes
    on = build(samples.DOORBELL, critical_enabled=True)
    assert on.payload["aps"]["interruption-level"] == "critical"
    assert on.payload["aps"]["sound"] == {"critical": 1, "name": "default", "volume": 0.6}
    assert on.payload["fenton"]["entity_id"] == "camera.front_door_camera"


def test_critical_level_without_a_critical_sound_gets_one() -> None:
    body = {"message": "Alarm", "data": {"push": {"interruption-level": "critical"}}}
    sound = build(body, critical_enabled=True).payload["aps"]["sound"]
    assert sound == {"critical": 1, "name": "default", "volume": 1.0}


@pytest.mark.parametrize(("sound", "expected"), [
    (None, "default"), ("none", None), ("None", None), ("chime.caf", "chime.caf"),
    ({"name": "none"}, None), ({"name": "bell.caf"}, "bell.caf"),
])
def test_sound(sound, expected) -> None:
    body = {"message": "x", "data": {"push": {} if sound is None else {"sound": sound}}}
    assert build(body, critical_enabled=False).payload["aps"].get("sound") == expected


def test_passive_is_low_priority_and_silent_when_asked() -> None:
    body = {"message": "Update", "data": {"push": {"interruption-level": "passive", "sound": "none"}}}
    request = build(body, critical_enabled=False)
    assert request.priority == 5 and "sound" not in request.payload["aps"]


def test_commands_become_background_pushes() -> None:
    request = build(samples.CLEAR, critical_enabled=False)
    assert request.push_type == "background" and request.priority == 5
    assert request.payload == {"aps": {"content-available": 1},
                               "fenton": {"v": 1, "command": "clear_notification",
                                          "tag": "sfenton-front-door-unsecured-42"}}
    assert build({"message": "request_location_update"}, critical_enabled=False).payload["fenton"]["command"] == \
        "request_location_update"


def test_unknown_keys_go_to_extra_and_android_keys_are_dropped() -> None:
    body = {"message": "x", "data": {"presentation_options": ["alert"], "sticky": True, "channel": "Doors",
                                     "fenton": {"category": "security"}}}
    fenton = build(body, critical_enabled=False).payload["fenton"]
    assert fenton["extra"] == {"presentation_options": ["alert"]}
    assert fenton["ext"] == {"category": "security"}


def test_bad_actions_are_dropped_not_fatal() -> None:
    body = {"message": "x", "data": {"actions": [{"title": "no id"}, {"action": "URI", "title": "No uri"},
                                                 {"action": "OK", "title": "Fine", "bogus": 1}]}}
    request = build(body, critical_enabled=False)
    assert request.payload["fenton"]["actions"] == [{"action": "OK", "title": "Fine"}]
    assert len(request.notes) == 2
    not_a_list = build({"message": "x", "data": {"actions": "nope"}}, critical_enabled=False)
    assert not_a_list.payload["aps"].get("category") is None


def test_media_without_actions_still_wakes_the_extension() -> None:
    body = {"message": "x", "data": {"image": "/local/snap.jpg", "attachment": {"url": "/media/a.mp4", "bad": 1}}}
    request = build(body, critical_enabled=False)
    assert request.payload["aps"]["mutable-content"] == 1
    assert request.payload["fenton"]["attachment"] == {"url": "/media/a.mp4"}


def test_long_tags_collapse_to_a_digest() -> None:
    tag = "t" * 80
    assert len(collapse_id(tag)) == 64 and collapse_id(tag) == collapse_id(tag)
    assert collapse_id("short") == "short"


def test_oversized_payloads() -> None:
    big_extra = {"message": "x", "data": {"blob": "y" * MAX_PAYLOAD_BYTES}}
    request = build(big_extra, critical_enabled=False)
    assert "extra" not in request.payload["fenton"] and request.notes
    with pytest.raises(PayloadError) as err:
        build({"message": "y" * MAX_PAYLOAD_BYTES}, critical_enabled=False)
    assert err.value.code == "payload_too_large"


def test_invalid_bodies() -> None:
    with pytest.raises(PayloadError):
        build([], critical_enabled=False)  # type: ignore[arg-type]
    with pytest.raises(PayloadError):
        build({"message": "x", "data": "nope"}, critical_enabled=False)


def test_headers() -> None:
    headers = build(samples.FRONT_DOOR_UNSECURED, critical_enabled=False).headers("com.sfenton.home")
    assert headers == {"apns-push-type": "alert", "apns-priority": "10", "apns-topic": "com.sfenton.home",
                       "apns-collapse-id": "sfenton-front-door-unsecured-42"}


def fixture_payload(name: str, body: dict, critical: bool) -> dict:
    """What ``tests/fixtures/<name>.apns`` holds: the payload plus simctl's target bundle."""
    payload = copy.deepcopy(build(body, critical_enabled=critical, now=samples.FIXTURE_NOW).payload)
    return {"Simulator Target Bundle": "com.sfenton.home", **payload}


def expected_fixtures() -> dict[str, dict]:
    fixtures = {name: fixture_payload(name, body, False) for name, body in samples.FIXTURES.items()}
    fixtures["doorbell-critical"] = fixture_payload("doorbell-critical", samples.DOORBELL, True)
    for name, body in live2_samples.cases().items():
        fixtures[name] = fixture_payload(name, body, False)
    return fixtures


def test_fixtures_are_current() -> None:
    """Regenerate with ``python tests/fixtures/build.py`` after changing the mapping."""
    for name, payload in expected_fixtures().items():
        assert json.loads((FIXTURES / f"{name}.apns").read_text()) == payload, name
