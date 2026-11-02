"""Turns the body Home Assistant's ``mobile_app`` posts to our ``push_url`` into an Apple push.

``mobile_app`` posts ``{message, title, data, push_token, registration_info, live_activity_token?}`` where
``data`` is the automation's notification data, written for the official companion app. This module maps it
to APNs headers and JSON per ``docs/CONTRACT.md``. It has no Home Assistant imports, so it is unit-tested alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .live import (
    ATTRIBUTES_TYPE,
    LIVE_CONTRACT_VERSION,
    MAX_DISMISS_AFTER,
    STATE_VERSION_V2,
    activity_attributes,
    block_version,
    default_stale,
    default_stale_v2,
    HOME_TAG,
    empty_state,
    ended_state_v2,
    generic_state,
    home_state,
    state_v2,
)

#: Bumped when the ``fenton`` block changes shape. Live Activity payloads carry ``LIVE_CONTRACT_VERSION`` instead.
CONTRACT_VERSION = 1

#: APNs refuses larger alert payloads.
MAX_PAYLOAD_BYTES = 4096
#: ``apns-collapse-id`` limit.
MAX_COLLAPSE_ID_BYTES = 64

DYNAMIC_CATEGORY = "fenton.dynamic"

#: ``message`` values the companion app treats as commands rather than text.
COMMANDS = frozenset({
    "clear_notification",
    "request_location_update",
    "update_widgets",
    "update_complications",
    "clear_badge",
})

INTERRUPTION_LEVELS = frozenset({"passive", "active", "time-sensitive", "critical"})

#: ``data`` keys mapped explicitly; everything else goes to ``fenton.extra`` unless it is Android-only.
_MAPPED = frozenset({
    "tag", "group", "url", "clickAction", "actions", "entity_id", "image", "video", "attachment",
    "push", "subtitle", "fenton", "live_update", "event", "fenton_activity",
})
_ANDROID_ONLY = frozenset({
    "sticky", "persistent", "channel", "importance", "ttl", "priority", "vibrationPattern", "ledColor",
    "color", "notification_icon", "icon_url", "visibility", "timeout", "car_ui", "alert_once",
    "chronometer", "when", "when_relative", "progress", "progress_max", "progress_indeterminate",
    "media_stream", "tts_text", "command", "intent_action", "intent_uri", "intent_package_name",
    "intent_extras", "intent_type", "intent_class_name", "group_alert_behavior",
})


class PayloadError(ValueError):
    """The notification can't be sent as given; ``code`` is stable, the message is for logs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ApnsRequest:
    """One APNs request: where it goes and what it says."""

    push_type: str
    priority: int
    payload: dict[str, Any]
    collapse_id: str | None = None
    #: Appended to the bundle id for ``apns-topic`` (Live Activities use ``.push-type.liveactivity``).
    topic_suffix: str = ""
    #: Which token in the body this request is addressed to.
    token_key: str = "push_token"
    #: Downgrades and drops, for the log.
    notes: list[str] = field(default_factory=list)
    #: Nothing is sent to Apple (the view still answers 201): a Live Activity update for a device that has no
    #: activity token and no push-to-start token must not turn into a banner.
    dropped: bool = False

    def headers(self, bundle_id: str) -> dict[str, str]:
        """APNs request headers (authorization is added by the client)."""
        headers = {
            "apns-push-type": self.push_type,
            "apns-priority": str(self.priority),
            "apns-topic": bundle_id + self.topic_suffix,
        }
        if self.collapse_id:
            headers["apns-collapse-id"] = self.collapse_id
        return headers

    def body(self) -> bytes:
        """The JSON body APNs receives."""
        return _encode(self.payload)


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def collapse_id(tag: str) -> str:
    """``tag`` itself when it fits APNs' 64-byte limit, else a stable digest of it."""
    if len(tag.encode()) <= MAX_COLLAPSE_ID_BYTES:
        return tag
    return hashlib.sha256(tag.encode()).hexdigest()


class Timestamps:
    """``aps.timestamp`` per activity, strictly increasing: ``max(ceil(now), last + 1)``.

    ActivityKit drops an update whose timestamp is not newer than the content it shows, and the app's optimistic write
    after a button press is stamped ``floor(now)``. Rounding up and never repeating one keeps a confirmation newer than
    the optimistic write and pushes for one activity from ever tying. Not persisted: a restart resets to the clock.
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def next(self, key: str, now: float) -> int:
        stamp = max(math.ceil(now), self._last.get(key, 0) + 1)
        self._last[key] = stamp
        return stamp


def build(body: dict[str, Any], *, critical_enabled: bool, now: float | None = None,
          timestamps: Timestamps | None = None, ts_key: str | None = None) -> ApnsRequest:
    """Map a ``mobile_app`` push body to an APNs request.

    ``timestamps`` and ``ts_key`` (the device and tag) make a Live Activity's ``timestamp`` strictly increasing.
    """
    if not isinstance(body, dict):
        raise PayloadError("invalid_body", "Body must be a JSON object")
    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise PayloadError("invalid_data", "data must be an object")
    message = body.get("message")
    message = "" if message is None else str(message)
    tag = _string(data.get("tag"))

    if body.get("live_activity_token"):
        return _live_activity(body, data, message, tag, now, timestamps, ts_key)
    if data.get("live_update"):
        return ApnsRequest(push_type="liveactivity", priority=5, payload={}, dropped=True,
                           notes=["live_update without a live_activity_token dropped (this device cannot show an activity)"])
    if message in COMMANDS:
        return _command(message, tag)
    return _alert(body, data, message, tag, critical_enabled)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _command(command: str, tag: str | None) -> ApnsRequest:
    fenton: dict[str, Any] = {"v": CONTRACT_VERSION, "command": command}
    if tag:
        fenton["tag"] = tag
    return ApnsRequest(push_type="background", priority=5, payload={"aps": {"content-available": 1}, "fenton": fenton})


def _alert(body: dict[str, Any], data: dict[str, Any], message: str, tag: str | None,
           critical_enabled: bool) -> ApnsRequest:
    push = data.get("push") if isinstance(data.get("push"), dict) else {}
    notes: list[str] = []

    alert: dict[str, Any] = {"body": message}
    if title := _string(body.get("title")):
        alert["title"] = title
    if subtitle := _string(data.get("subtitle")) or _string(push.get("subtitle")):
        alert["subtitle"] = subtitle
    aps: dict[str, Any] = {"alert": alert}

    level = _string(push.get("interruption-level"))
    if level is not None and level not in INTERRUPTION_LEVELS:
        notes.append(f"unknown interruption-level {level!r} ignored")
        level = None
    sound, critical_sound = _sound(push.get("sound"))
    if critical_sound or level == "critical":
        if critical_enabled:
            level = "critical"
            if not isinstance(sound, dict):
                sound = {"critical": 1, "name": sound if isinstance(sound, str) else "default", "volume": 1.0}
        else:
            notes.append("critical downgraded to time-sensitive (critical alerts are off)")
            level = "time-sensitive"
            sound = "default"
    if sound is not None:
        aps["sound"] = sound
    if level:
        aps["interruption-level"] = level
    if (badge := push.get("badge")) is not None:
        try:
            aps["badge"] = int(badge)
        except (TypeError, ValueError):
            notes.append("non-numeric badge ignored")
    if (score := push.get("relevance-score")) is not None:
        try:
            aps["relevance-score"] = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            notes.append("non-numeric relevance-score ignored")

    group = _string(data.get("group")) or _string(push.get("thread-id"))
    if group:
        aps["thread-id"] = group

    fenton: dict[str, Any] = {"v": CONTRACT_VERSION}
    if tag:
        fenton["tag"] = tag
    if group:
        fenton["group"] = group
    if url := _string(data.get("url")) or _string(data.get("clickAction")):
        fenton["url"] = url
    actions = _actions(data.get("actions"), notes)
    if actions:
        fenton["actions"] = actions
        aps["category"] = DYNAMIC_CATEGORY
    elif category := _string(push.get("category")):
        aps["category"] = category
    has_media = False
    for key in ("entity_id", "image", "video"):
        if value := _string(data.get(key)):
            fenton[key] = value
            has_media = True
    attachment = data.get("attachment")
    if isinstance(attachment, dict) and _string(attachment.get("url")):
        fenton["attachment"] = {k: v for k, v in attachment.items() if k in ("url", "content-type", "hide-thumbnail")}
        has_media = True
    if isinstance(data.get("fenton"), dict):
        fenton["ext"] = data["fenton"]
    extra = {k: v for k, v in data.items() if k not in _MAPPED and k not in _ANDROID_ONLY}
    if extra:
        fenton["extra"] = extra
    if actions or has_media or "ext" in fenton:
        aps["mutable-content"] = 1

    payload = {"aps": aps, "fenton": fenton}
    if len(_encode(payload)) > MAX_PAYLOAD_BYTES and "extra" in fenton:
        del fenton["extra"]
        notes.append("extra data dropped to fit Apple's 4 KB limit")
    if len(_encode(payload)) > MAX_PAYLOAD_BYTES:
        raise PayloadError("payload_too_large", "Notification is larger than Apple's 4 KB limit")

    priority = 5 if level == "passive" else 10
    return ApnsRequest(push_type="alert", priority=priority, payload=payload,
                       collapse_id=collapse_id(tag) if tag else None, notes=notes)


def _sound(value: Any) -> tuple[str | dict[str, Any] | None, bool]:
    """(APNs sound, whether a critical sound was asked for). The companion app plays ``default`` when unset."""
    if value is None:
        return "default", False
    if isinstance(value, dict):
        critical = bool(value.get("critical"))
        name = _string(value.get("name")) or "default"
        if not critical:
            return (None if name == "none" else name), False
        try:
            volume = max(0.0, min(1.0, float(value.get("volume", 1.0))))
        except (TypeError, ValueError):
            volume = 1.0
        return {"critical": 1, "name": name, "volume": volume}, True
    name = _string(value)
    if name is None or name.lower() == "none":
        return None, False
    return name, False


_ACTION_KEYS = (
    "action", "title", "uri", "icon", "authenticationRequired", "destructive", "activationMode",
    "behavior", "textInputButtonTitle", "textInputPlaceholder",
)


def _actions(value: Any, notes: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        notes.append("actions must be a list; ignored")
        return []
    actions = []
    for raw in value:
        if not isinstance(raw, dict) or not _string(raw.get("action")) or not _string(raw.get("title")):
            notes.append("action without an id or title dropped")
            continue
        action = {k: raw[k] for k in _ACTION_KEYS if k in raw and raw[k] is not None}
        if action["action"] == "URI" and not _string(action.get("uri")):
            notes.append("URI action without a uri dropped")
            continue
        actions.append(action)
    if len(actions) > 10:
        notes.append("more than 10 actions; extra ones dropped")
        actions = actions[:10]
    return actions


def _live_activity(body: dict[str, Any], data: dict[str, Any], message: str, tag: str | None,
                   now: float | None, timestamps: Timestamps | None = None, ts_key: str | None = None) -> ApnsRequest:
    """A Live Activity start, update or end (contract v2, section 6).

    ``mobile_app`` decides the event (start with the push-to-start token, update with the activity's token, end on
    ``clear_notification``) and posts the token as ``live_activity_token``. The content is the automation's
    ``fenton_activity`` block when it sent one, else the companion app's ``live_update`` keys as one generic job.
    """
    if not tag:
        raise PayloadError("tag_required", "Live Activities need a tag")
    clock = now if now is not None else time.time()
    timestamp = timestamps.next(ts_key or tag, clock) if timestamps is not None else math.ceil(clock)
    event = _string(data.get("event")) or "update"
    if event not in ("start", "update", "end"):
        raise PayloadError("invalid_event", f"Unknown Live Activity event {event!r}")
    title = _string(body.get("title")) or ""
    notes: list[str] = []

    stale_ts: int | None = None
    block = data.get("fenton_activity")
    version = block_version(block)
    if block is not None:
        try:
            state, stale_ts = (state_v2 if version == STATE_VERSION_V2 else home_state)(block, notes)
        except ValueError as err:
            raise PayloadError("invalid_activity", str(err)) from err
    elif event == "end" and message in COMMANDS:
        # ``clear_notification``: the Home activity (v1) ends with an empty list, a per-job activity with a v2 stub.
        state = empty_state() if tag == HOME_TAG else ended_state_v2()
    else:
        state = generic_state(tag, title, message, data, timestamp, notes)
        if event == "end":
            for job in state["jobs"]:
                job["phase"] = "done"
                job.pop("end_ts", None)
    kind = "home" if block is not None and version != STATE_VERSION_V2 else "generic"

    aps: dict[str, Any] = {"timestamp": timestamp, "event": event, "content-state": state}
    alerting = False
    if event == "start":
        aps["attributes-type"] = ATTRIBUTES_TYPE
        attributes: dict[str, Any] = {"tag": tag, "title": _title(title, version), "kind": kind}
        if version == STATE_VERSION_V2:
            attributes.update(activity_attributes(data.get("fenton_attributes")))
        aps["attributes"] = attributes
        # A start must alert (Apple); this one is quiet unless ``live_alert`` asks for the default sound (a critical
        # problem that starts the card). iOS 18 hands the new activity's token back.
        aps["alert"] = {"title": _title(title, version), "body": message or title or "Started"}
        if data.get("live_alert"):
            aps["sound"] = "default"
        aps["input-push-token"] = 1
        alerting = True
    elif data.get("live_alert"):
        aps["alert"] = {"title": title or "Home", "body": message or title or "Update"}
        aps["sound"] = "default"
        alerting = True
    if event == "end":
        aps["dismissal-date"] = timestamp + _dismiss_after(data.get("dismiss_after"), notes)
    else:
        default = default_stale_v2(state, timestamp) if version == STATE_VERSION_V2 else default_stale(state, timestamp)
        aps["stale-date"] = stale_ts if stale_ts and stale_ts > timestamp else default
    if (score := data.get("relevance_score")) is not None:
        try:
            aps["relevance-score"] = float(score)
        except (TypeError, ValueError):
            notes.append("non-numeric relevance_score ignored")

    payload = {"aps": aps, "fenton": {"v": LIVE_CONTRACT_VERSION, "tag": tag}}
    if len(_encode(payload)) > MAX_PAYLOAD_BYTES:
        raise PayloadError("payload_too_large", "Live Activity update is larger than Apple's 4 KB limit")
    # Apple budgets priority-10 pushes: starts, ends, anything that alerts and anything marked urgent use it; the
    # many progress updates in between go at 5 and may be delayed.
    priority = 10 if event in ("start", "end") or alerting or data.get("urgent") else 5
    return ApnsRequest(push_type="liveactivity", priority=priority, payload=payload, notes=notes,
                       topic_suffix=".push-type.liveactivity", token_key="live_activity_token")


def _title(title: str, version: int) -> str:
    """The activity's static title: the device's name for a job (capped at 32), "Home" for the old aggregate."""
    if version == STATE_VERSION_V2:
        return (title or "Home")[:32]
    return title or "Home"


def _dismiss_after(value: Any, notes: list[str]) -> int:
    """Seconds an ended activity stays on the Lock Screen: 0 (now) by default, four hours at most."""
    if value is None:
        return 0
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        notes.append("invalid dismiss_after ignored")
        return 0
    if value > MAX_DISMISS_AFTER:
        notes.append("dismiss_after capped at four hours")
    return int(min(value, MAX_DISMISS_AFTER))
