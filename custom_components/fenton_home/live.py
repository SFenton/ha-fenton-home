"""The Home Live Activity's content: what ``mobile_app`` posts becomes the ActivityKit ``content-state``.

Contract v2, section 6 of ``docs/CONTRACT.md``. The shape is the Fenton Home app's ``HomeActivityState``
(``FentonActivityModel``): ``{"v": 1, "focus"?, "url"?, "jobs": [...]}``. This module has no Home Assistant imports, so
it is unit-tested alone, and it only ever *writes* what the app's decoder reads: unknown keys are dropped, numbers
stay numbers (ActivityKit decodes pushes with a default ``JSONDecoder``, so dates are Unix seconds, never strings),
and everything is cut to the budget that keeps a push under Apple's 4 KB.
"""

from __future__ import annotations

from typing import Any

#: The Swift type name; a push-to-start token is per attributes type, so it is also the wire ``attributes-type``.
ATTRIBUTES_TYPE = "HomeActivityAttributes"
#: Bumped when the ``fenton`` block of a Live Activity payload changes shape (alerts and commands stay on v1).
LIVE_CONTRACT_VERSION = 2
#: ``HomeActivityState.currentVersion``.
STATE_VERSION = 1
#: The tag of the aggregate Home activity, which ``mobile_app`` keys the update token by.
HOME_TAG = "fenton-home"

MAX_JOBS = 6
MAX_CONTROLS = 2
MAX_STATUS = 40
MAX_TEXT = 40
MAX_URL = 200
#: Seconds past the earliest end before the views read "Updating…".
STALE_GRACE = 600
#: The stale window of a list with no end times.
OPEN_ENDED_STALE = 1800
#: ActivityKit keeps an ended activity on the Lock Screen for four hours at most.
MAX_DISMISS_AFTER = 4 * 3600

PHASES = frozenset({"running", "paused", "waiting", "attention", "stopping", "done"})
ROLES = frozenset({"normal", "destructive"})

#: SF Symbols for the companion app's ``notification_icon`` values worth mapping; anything else is a plain circle.
_ICONS = {
    "mdi:dishwasher": "dishwasher",
    "mdi:washing-machine": "washer",
    "mdi:tumble-dryer": "dryer",
    "mdi:fan": "fan",
    "mdi:robot-vacuum": "fan",
    "mdi:timer": "timer",
    "mdi:timer-outline": "timer",
    "mdi:sprinkler": "drop.fill",
    "mdi:water": "drop.fill",
    "mdi:bell": "bell.fill",
    "mdi:alarm": "alarm.fill",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any, limit: int) -> str:
    """``value`` as text no longer than ``limit``, ending in an ellipsis when cut."""
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _seconds(value: Any) -> int | None:
    """Unix seconds from a number (milliseconds are recognised and converted), else None."""
    if not _is_number(value) or value <= 0:
        return None
    return int(value / 1000) if value > 1e11 else int(value)


def _control(raw: Any) -> dict[str, str] | None:
    """A control as the phone may see it: id, title, role. The service it maps to never leaves Home Assistant."""
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
        return None
    control = {"id": _text(raw["id"], MAX_TEXT), "title": _text(raw["title"], MAX_TEXT)}
    if raw.get("role") in ROLES and raw["role"] != "normal":
        control["role"] = raw["role"]
    return control


def _job(raw: Any, notes: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
        notes.append("job without an id or title dropped")
        return None
    phase = raw.get("phase")
    if phase not in PHASES:
        if phase is not None:
            notes.append(f"unknown phase {phase!r} read as running")
        phase = "running"
    job: dict[str, Any] = {
        "id": _text(raw["id"], MAX_TEXT),
        "kind": _text(raw.get("kind") or "generic", MAX_TEXT),
        "title": _text(raw["title"], MAX_TEXT),
        "status": _text(raw.get("status"), MAX_STATUS),
        "phase": phase,
        "symbol": _text(raw.get("symbol") or "circle", MAX_TEXT),
        "tint": _text(raw.get("tint") or "neutral", MAX_TEXT),
    }
    for key in ("start_ts", "end_ts"):
        if (seconds := _seconds(raw.get(key))) is not None:
            job[key] = seconds
    if raw.get("estimated") is True:
        job["estimated"] = True
    if _is_number(raw.get("progress")):
        job["progress"] = round(min(max(float(raw["progress"]), 0.0), 1.0), 3)
    controls = [c for c in (_control(c) for c in (raw.get("controls") or [])) if c]
    if len(controls) > MAX_CONTROLS:
        notes.append("more than 2 controls on a job; extra ones dropped")
    if controls:
        job["controls"] = controls[:MAX_CONTROLS]
    return job


def home_state(block: Any, notes: list[str]) -> tuple[dict[str, Any], int | None]:
    """Validate an automation's ``fenton_activity`` block into a ``content-state``.

    Returns the state and the block's ``stale_ts`` (Unix seconds), which belongs in ``aps.stale-date`` rather than in
    the state. Raises ``ValueError`` when the block is not an object with a list of jobs.
    """
    if not isinstance(block, dict) or not isinstance(block.get("jobs"), list):
        raise ValueError("fenton_activity must be an object with a jobs list")
    jobs = [j for j in (_job(raw, notes) for raw in block["jobs"]) if j]
    focus = block.get("focus")
    if len(jobs) > MAX_JOBS:
        notes.append("more than 6 jobs; extra ones dropped")
        kept = jobs[:MAX_JOBS]
        focused = next((j for j in jobs if j["id"] == focus), None)
        if focused and focused not in kept:
            kept[-1] = focused
        jobs = kept
    state: dict[str, Any] = {"v": STATE_VERSION, "jobs": jobs}
    if focus and any(j["id"] == focus for j in jobs):
        state["focus"] = focus
    if isinstance(block.get("url"), str) and block["url"]:
        url = block["url"].strip()
        if len(url) > MAX_URL or "token=" in url.lower():
            notes.append("url dropped (too long, or it carries a token)")
        else:
            state["url"] = url
    return state, _seconds(block.get("stale_ts"))


def generic_state(tag: str, title: str, message: str, data: dict[str, Any], now: int, notes: list[str]) -> dict[str, Any]:
    """One ``generic`` job from the companion app's ``live_update`` keys, so existing automations show something sensible.

    ``message`` is the status line, ``progress``/``progress_max`` the bar, ``chronometer`` with ``when`` (or
    ``when_relative``) a countdown. A count-up chronometer (a time already past) is not drawn.
    """
    job: dict[str, Any] = {
        "id": _text(tag, MAX_TEXT),
        "kind": "generic",
        "title": _text(title or "Home", MAX_TEXT),
        "status": _text(message, MAX_STATUS),
        "phase": "running",
        "symbol": _ICONS.get(str(data.get("notification_icon") or ""), "circle"),
        "tint": "neutral",
    }
    if data.get("chronometer") and _is_number(data.get("when")):
        end = now + int(data["when"]) if data.get("when_relative") else _seconds(data["when"])
        if end is not None and end > now:
            job["start_ts"] = now
            job["end_ts"] = end
    progress, maximum = data.get("progress"), data.get("progress_max")
    if _is_number(progress) and _is_number(maximum) and maximum > 0:
        job["progress"] = round(min(max(progress / maximum, 0.0), 1.0), 3)
    state: dict[str, Any] = {"v": STATE_VERSION, "jobs": [job]}
    if isinstance(data.get("url"), str) and data["url"].strip() and "token=" not in data["url"].lower():
        state["url"] = data["url"].strip()[:MAX_URL]
    return state


def empty_state() -> dict[str, Any]:
    """The content of an activity that ends with nothing left to show."""
    return {"v": STATE_VERSION, "jobs": []}


def default_stale(state: dict[str, Any], now: int) -> int:
    """Earliest end of a running job plus ten minutes, or half an hour from now when no job has an end."""
    ends = [j["end_ts"] for j in state["jobs"] if j.get("phase") != "done" and "end_ts" in j]
    return max(min(ends), now) + STALE_GRACE if ends else now + OPEN_ENDED_STALE


# ---------------------------------------------------------------------------------------------------------------------
# Content state v2 (one activity per job; spec ``live-activities-per-job.md`` section 3)

#: ``HomeActivityState`` v2: the content of one job's activity.
STATE_VERSION_V2 = 2

PHASES_V2 = frozenset({"running", "paused", "waiting", "problem", "done"})
VALUE_KINDS = frozenset({"countdown", "countup", "percent", "text"})
ROLES_V2 = frozenset({"primary", "secondary"})
SEVERITIES = frozenset({"critical", "warning"})
#: The app's problem glyph set (section 5).
PROBLEM_ICONS = frozenset({"alert", "door", "dock", "edge", "brush", "battery", "water", "waste", "mop", "bag",
                           "question", "door_dish"})
#: Keys the relay understands in a v2 block; anything else is dropped. ``pending`` is the app's alone, so it is never
#: forwarded. ``stale_ts`` becomes ``aps.stale-date``.
KEYS_V2 = frozenset({"v", "phase", "headline", "detail", "short", "value", "progress", "controls", "problem", "url", "notice", "stale_ts"})

MAX_HEADLINE = 32
MAX_DETAIL = 32
MAX_SHORT = 10
MAX_VALUE_TEXT = 16
MAX_PROBLEM = 40
MAX_MESSAGE = 90
MAX_CHIP = 20
MAX_NOTICE = 60
MAX_CONTROL_TITLE = 20
MAX_CONTROLS_V2 = 2
MAX_CONTROLS_PROBLEM = 3
#: Attributes the app decodes with defaults: ``kind``, ``symbol`` and ``tint`` (spec section 3).
ATTRIBUTE_KEYS = ("kind", "symbol", "tint")


def _required_seconds(value: Any, name: str) -> int:
    seconds = _seconds(value)
    if seconds is None:
        raise ValueError(f"{name} must be a Unix time in seconds")
    return seconds


def _value_v2(raw: Any, progress: float | None, notes: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in VALUE_KINDS:
        raise ValueError("value.kind must be countdown, countup, percent or text")
    kind = raw["kind"]
    value: dict[str, Any] = {"kind": kind}
    if kind == "countdown":
        value["end_ts"] = _required_seconds(raw.get("end_ts"), "value.end_ts")
        if (start := _seconds(raw.get("start_ts"))) is not None and start < value["end_ts"]:
            value["start_ts"] = start
    elif kind == "countup":
        value["start_ts"] = _required_seconds(raw.get("start_ts"), "value.start_ts")
        if _is_number(raw.get("paused_elapsed_s")) and raw["paused_elapsed_s"] >= 0:
            value["paused_elapsed_s"] = int(raw["paused_elapsed_s"])
    elif kind == "percent":
        if progress is None:
            raise ValueError("a percent value needs progress")
    else:
        label = _text(raw.get("text"), MAX_VALUE_TEXT)
        if not label:
            raise ValueError("a text value needs text")
        if len(str(raw.get("text")).strip()) > MAX_VALUE_TEXT:
            notes.append("value.text cut to 16 characters")
        value["text"] = label
    if raw.get("estimated") is True:
        value["estimated"] = True
    return value


def _control_v2(raw: Any, index: int) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
        raise ValueError("a control needs an id and a title")
    control = {"id": _text(raw["id"], MAX_TEXT), "title": _text(raw["title"], MAX_CONTROL_TITLE)}
    if raw.get("role") in ROLES_V2 and raw["role"] != "primary" and index > 0:
        control["role"] = raw["role"]
    return control


def _problem_v2(raw: Any, notes: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("severity") not in SEVERITIES:
        raise ValueError("problem.severity must be critical or warning")
    if not raw.get("code") or not raw.get("problem") or not raw.get("message"):
        raise ValueError("a problem needs a code, a problem line and a message")
    icon = raw.get("icon")
    if icon not in PROBLEM_ICONS:
        notes.append(f"unknown problem icon {icon!r} read as question")
        icon = "question"
    problem: dict[str, Any] = {
        "severity": raw["severity"], "code": _text(raw["code"], MAX_TEXT * 2), "icon": icon,
        "problem": _text(raw["problem"], MAX_PROBLEM), "message": _text(raw["message"], MAX_MESSAGE),
        "chips": [_text(chip, MAX_CHIP) for chip in (raw.get("chips") or [])[:1] if chip],
    }
    if isinstance(raw.get("task_id"), int) and not isinstance(raw["task_id"], bool):
        problem["task_id"] = raw["task_id"]
    return problem


def state_v2(block: Any, notes: list[str]) -> tuple[dict[str, Any], int | None]:
    """Validate a v2 ``fenton_activity`` block (one job's content state).

    Returns the state and the block's ``stale_ts`` (Unix seconds). Unknown keys are dropped (``pending`` is the app's
    own and is never forwarded); a structurally wrong block raises ``ValueError``.
    """
    if not isinstance(block, dict):
        raise ValueError("fenton_activity must be an object")
    for key in block:
        if key not in KEYS_V2:
            notes.append("pending is written by the app only and was dropped" if key == "pending"
                         else f"unknown key {key!r} dropped")
    phase = block.get("phase")
    if phase not in PHASES_V2:
        raise ValueError(f"phase must be one of {', '.join(sorted(PHASES_V2))}")
    progress = round(min(max(float(block["progress"]), 0.0), 1.0), 3) if _is_number(block.get("progress")) else None
    state: dict[str, Any] = {"v": STATE_VERSION_V2, "phase": phase}
    if headline := _text(block.get("headline"), MAX_HEADLINE):
        state["headline"] = headline
    if detail := _text(block.get("detail"), MAX_DETAIL):
        state["detail"] = detail
    if short := _text(block.get("short"), MAX_SHORT):
        state["short"] = short
    if block.get("value") is not None:  # a vacuum card has no time to show
        state["value"] = _value_v2(block.get("value"), progress, notes)
    if progress is not None:
        state["progress"] = progress
    controls_raw = block.get("controls") or []
    if not isinstance(controls_raw, list):
        raise ValueError("controls must be a list")
    limit = MAX_CONTROLS_PROBLEM if phase == "problem" else MAX_CONTROLS_V2
    if len(controls_raw) > limit:
        raise ValueError(f"a {'problem ' if phase == 'problem' else ''}card carries at most {limit} controls")
    state["controls"] = [_control_v2(c, i) for i, c in enumerate(controls_raw)]
    if (block.get("problem") is not None) != (phase == "problem"):
        raise ValueError("problem is present exactly when phase is problem")
    if phase == "problem":
        state["problem"] = _problem_v2(block["problem"], notes)
    if isinstance(block.get("url"), str) and block["url"].strip():
        url = block["url"].strip()
        if len(url) > MAX_URL or "token=" in url.lower():
            notes.append("url dropped (too long, or it carries a token)")
        else:
            state["url"] = url
    if notice := _text(block.get("notice"), MAX_NOTICE):
        state["notice"] = notice
    return state, _seconds(block.get("stale_ts"))


def default_stale_v2(state: dict[str, Any], now: int) -> int:
    """Ten minutes after a countdown ends, else half an hour from now."""
    value = state.get("value") or {}
    if value.get("kind") == "countdown" and value["end_ts"] > now:
        return value["end_ts"] + STALE_GRACE
    return now + OPEN_ENDED_STALE


def activity_attributes(raw: Any) -> dict[str, str]:
    """The static attributes beyond tag and title that ride on a start: ``kind``, ``symbol`` and ``tint``."""
    if not isinstance(raw, dict):
        return {}
    return {key: _text(raw[key], MAX_TEXT) for key in ATTRIBUTE_KEYS if raw.get(key)}


def block_version(block: Any) -> int:
    """The content-state version of a block: 2 for one-job content, else 1 (the Home activity)."""
    return STATE_VERSION_V2 if isinstance(block, dict) and block.get("v") == STATE_VERSION_V2 else STATE_VERSION


def ended_state_v2() -> dict[str, Any]:
    """The content of a per-job activity that ends with nothing left to show (``clear_notification``)."""
    return {"v": STATE_VERSION_V2, "phase": "done", "value": {"kind": "text", "text": "Ended"}, "controls": []}
