"""Notification bodies as Home Assistant's mobile_app posts them, modelled on the household's real automations.

Shared by the unit tests and ``tests/fixtures`` (the APNs payloads the app tests against with ``simctl push``).
"""

REGISTRATION_INFO = {"app_id": "com.sfenton.home", "app_version": "0.1.0 (1)", "os_version": "27.0",
                     "webhook_id": "hook-stephen-iphone"}

FRONT_DOOR_UNSECURED = {
    "message": "The front door is unlocked and nobody is home. Lock it from here or open Security.",
    "title": "Front Door · Unlocked While Away",
    "data": {
        "tag": "sfenton-front-door-unsecured-42",
        "group": "front-door-unsecured",
        "url": "/sfenton-react-dash/home?path=security",
        "clickAction": "/sfenton-react-dash/home?path=security",
        "alert_once": False,
        "push": {"interruption-level": "time-sensitive", "sound": "default"},
        "actions": [
            {"action": "FRONT_DOOR_LOCK_42", "title": "Confirm: Lock Front Door", "authenticationRequired": True,
             "activationMode": "background", "icon": "sfsymbols:lock"},
            {"action": "URI", "title": "Open Security", "uri": "/sfenton-react-dash/home?path=security",
             "icon": "sfsymbols:shield"},
        ],
    },
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

DOORBELL = {
    "message": "Someone is at the front door.",
    "title": "Doorbell",
    "data": {
        "tag": "doorbell",
        "url": "/sfenton-react-dash/home?path=security#camera-front-door",
        "entity_id": "camera.front_door_camera",
        "push": {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 0.6}},
        "actions": [{"action": "URI", "title": "Open Camera",
                     "uri": "/sfenton-react-dash/home?path=security#camera-front-door"}],
    },
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

DONETICK = {
    "message": "Empty the dishwasher is past due.",
    "title": "Chore past due",
    "data": {
        "tag": "donetick_task_17",
        "url": "/sfenton-react-dash/home?path=chores",
        "clickAction": "/sfenton-react-dash/home?path=chores",
        "push": {"sound": "default", "interruption-level": "active"},
        "actions": [
            {"action": "DONETICK_COMPLETE_17", "title": "Complete"},
            {"action": "DONETICK_SNOOZE_1H_17", "title": "Snooze 1 hour"},
            {"action": "DONETICK_SNOOZE_1D_17", "title": "Snooze 1 day"},
        ],
    },
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

CLEAR = {
    "message": "clear_notification",
    "data": {"tag": "sfenton-front-door-unsecured-42"},
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

#: The instant the live fixtures are built at (2026-10-17 15:00:00 UTC), so ``tests/fixtures/live-*.apns`` are stable.
FIXTURE_NOW = 1_792_249_200

#: What the ``fenton_home.set_live_job`` engine will put in ``data.fenton_activity`` (Phase 2); the automations of
#: Phase 1 can send the same block by hand.
HOME_ACTIVITY = {
    "focus": "dishwasher",
    "url": "/sfenton-react-dash/home?path=kitchen",
    "jobs": [
        {"id": "dishwasher", "kind": "dishwasher", "title": "Dishwasher", "status": "Eco 50° · Drying", "phase": "running",
         "start_ts": FIXTURE_NOW - 8_000, "end_ts": FIXTURE_NOW + 2_520, "progress": 0.62, "symbol": "dishwasher",
         "tint": "cleaning",
         # ``action`` and ``target`` are Home Assistant's own: they must not reach the phone.
         "controls": [{"id": "stop", "title": "Stop", "role": "destructive", "action": "button.press",
                       "target": {"entity_id": "button.dishwasher_stop_program"}}]},
        {"id": "fan.master", "kind": "bathroom_fan", "title": "Master Bath Fan", "status": "Off in", "phase": "running",
         "start_ts": FIXTURE_NOW - 300, "end_ts": FIXTURE_NOW + 1_500, "symbol": "fan", "tint": "air",
         "controls": [{"id": "cancel_timer", "title": "Cancel Timer"}]},
    ],
}

LIVE_START = {
    "message": "Dishwasher started",
    "title": "Home",
    "live_activity_token": "b" * 64,
    "data": {"tag": "fenton-home", "live_update": True, "event": "start", "fenton_activity": HOME_ACTIVITY},
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

LIVE_UPDATE = {
    "message": "Dishwasher · 42 min left",
    "title": "Home",
    "live_activity_token": "c" * 64,
    "data": {"tag": "fenton-home", "live_update": True, "event": "update", "fenton_activity": HOME_ACTIVITY},
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

LIVE_END = {
    "message": "clear_notification",
    "live_activity_token": "c" * 64,
    "data": {"tag": "fenton-home", "event": "end"},
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

#: A companion-app style automation: one chronometer notification, nothing Fenton-specific.
LIVE_GENERIC = {
    "message": "Sprinklers · Front lawn",
    "title": "Watering",
    "live_activity_token": "b" * 64,
    "data": {"tag": "sprinklers-front", "live_update": True, "event": "start", "chronometer": True, "when": 600,
             "when_relative": True, "progress": 1, "progress_max": 4, "notification_icon": "mdi:sprinkler",
             "url": "/sfenton-react-dash/home?path=sprinklers"},
    "push_token": "a" * 64,
    "registration_info": REGISTRATION_INFO,
}

FIXTURES = {
    "front-door-unsecured": FRONT_DOOR_UNSECURED,
    "doorbell": DOORBELL,
    "donetick": DONETICK,
    "clear-notification": CLEAR,
    "live-start": LIVE_START,
    "live-update": LIVE_UPDATE,
    "live-end": LIVE_END,
    "live-generic": LIVE_GENERIC,
}
