"""The household's entity ids, wording and service mappings, in one place.

Everything the job engine knows about the owner's Home Assistant lives here as data, so a wrong entity id or
service name is a one-line fix and never a logic change. Names come from the Live Activities per-job spec
(``Fenton-HASS-Native/.agents/platforms/apple/live-activities-per-job.md``, sections 4, 4a, 5 and 7); the ones the
spec does not give (marked ``VERIFY``) are best guesses the planner checks against the real configuration before
deploying. No Home Assistant imports, so it is shared by the unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------------------------------------------------
# Timings and limits (spec sections 4, 6 and 7)

#: Seconds a job that finished on its own shows "Complete" before the card ends.
LINGER_SECONDS = 300
#: ActivityKit ends an activity after 8 hours; the engine ends and restarts it at 7 h 45 min.
MAX_AGE_SECONDS = 7 * 3600 + 45 * 60
#: At most one push per tag per device in this many seconds; a burst collapses to the latest state.
COALESCE_SECONDS = 2.0
#: Slots per device: five activities observed, four for jobs and one for a problem.
SLOTS_TOTAL = 5
SLOTS_JOBS = 4
#: A control waits this long for the device to reach the expected state before the card says "Couldn't ...".
CONFIRM_TIMEOUT = 15.0
#: Re-push an unchanged open-ended card this often so its stale date (30 min) never passes.
HEARTBEAT_SECONDS = 25 * 60
OPEN_ENDED_STALE_SECONDS = 30 * 60
#: After Home Assistant starts, cards the engine finds no job for are kept this long: other integrations' entities
#: (Home Connect, the vacuums) are not there yet, and ending every card would only start them again.
STARTUP_HOLD_SECONDS = 120.0
#: A manual vacuum run's card changes room only after the sensors have read the new room this long. The robot's "estimated
#: segment" follows its position, so crossing a doorway flips it for a few seconds (the music-room robot's flips back
#: through its own room lasted 20 to 30 s every night of the week to 2026-11-13; the shortest real stay was 87 s).
SEGMENT_SETTLE_SECONDS = 60.0
#: Seconds after the engine ends a card during which a "dismissed" report is the app echoing our own end.
DISMISS_ECHO_SECONDS = 15.0
#: How long a service-raised problem with no task and no sensor confirmation stays before it is dropped.
PROBLEM_GRACE_SECONDS = 120.0

# ---------------------------------------------------------------------------------------------------------------------
# Jobs

JOB_FAN_GUEST = "fan.guest"
JOB_FAN_MASTER = "fan.master"
JOB_DISHWASHER = "dishwasher"
JOB_DRYER = "dryer"
JOB_WASHER = "washer"
JOB_HOT_FLASH = "hot_flash"
JOB_VAC_MAIN = "vacuum.main_floor"
JOB_VAC_MUSIC = "vacuum.music_room"
JOB_VAC_THEATER = "vacuum.theater_room"

JOB_IDS = (JOB_FAN_GUEST, JOB_FAN_MASTER, JOB_DISHWASHER, JOB_DRYER, JOB_WASHER, JOB_VAC_MAIN, JOB_VAC_MUSIC,
           JOB_VAC_THEATER, JOB_HOT_FLASH)

TAG_PREFIX = "job."


def tag_for(job_id: str) -> str:
    return TAG_PREFIX + job_id


def job_for_tag(tag: str) -> str | None:
    return tag[len(TAG_PREFIX):] if tag.startswith(TAG_PREFIX) else None


@dataclass(frozen=True)
class Style:
    """The static attributes of a job's activity: what the card is called and how it is drawn."""

    title: str
    kind: str
    symbol: str
    tint: str
    url: str


# ``symbol`` is a glyph id from the app's own set and ``tint`` a ``ColorFamily`` raw value the app knows (``air``,
# ``cleaning``, ``cooling``; an unknown family draws grey). Verified against the app and the dashboard in review.
DASH = "/sfenton-react-dash/home?path="
STYLES: dict[str, Style] = {
    JOB_FAN_GUEST: Style("Guest Bath Fan", "fan", "fan", "air", DASH + "guest-bathroom"),
    JOB_FAN_MASTER: Style("Master Bath Fan", "fan", "fan", "air", DASH + "master-bathroom"),
    JOB_DISHWASHER: Style("Dishwasher", "dishwasher", "dishwasher", "cleaning", DASH + "kitchen"),
    JOB_DRYER: Style("Dryer", "dryer", "dryer", "cleaning", DASH + "garage"),
    JOB_WASHER: Style("Washing Machine", "washer", "washer", "cleaning", DASH + "garage"),
    JOB_HOT_FLASH: Style("Hot Flash", "hot_flash", "hot_flash", "cooling", DASH + "master-bedroom"),
    JOB_VAC_MAIN: Style("Main Floor Vacuum", "vacuum", "vacuum", "cleaning", DASH + "living-room#robot-vacuum"),
    JOB_VAC_MUSIC: Style("Music Room Vacuum", "vacuum", "vacuum", "cleaning", DASH + "music-room#robot-vacuum"),
    JOB_VAC_THEATER: Style("Theater Room Vacuum", "vacuum", "vacuum", "cleaning", DASH + "theater-room#robot-vacuum"),
}

# ---------------------------------------------------------------------------------------------------------------------
# Bathroom fans (spec section 4)

FAN_ROOMS = {JOB_FAN_GUEST: "guest", JOB_FAN_MASTER: "master"}
#: Seconds the fan's ``ends_at`` may move before the card is updated.
FAN_END_TOLERANCE = 30
#: Stop is ``power off`` (spec section 4). If the planner finds that ``power off`` leaves the timer pending, set this and
#: Stop cancels the timer first, then turns the fan off.
FAN_STOP_CANCELS_TIMER_FIRST = False


def fan_entities(room: str) -> dict[str, str]:
    return {
        "pending": f"input_boolean.{room}_bathroom_fan_timer_pending",
        "ends_at": f"input_datetime.{room}_bathroom_fan_timer_ends_at",
        "command": f"{room}_bathroom_fan_command",  # script.<room>_bathroom_fan_command
    }


# ---------------------------------------------------------------------------------------------------------------------
# Dishwasher (spec section 4; entities verified 2026-10-17 in live-activities.md section 8)

DISHWASHER = {
    "state": "sensor.dishwasher_operation_state",
    "finish": "sensor.dishwasher_program_finish_time",
    "progress": "sensor.dishwasher_program_progress",
    "remote": "binary_sensor.dishwasher_remote_control",
    "stop": "button.dishwasher_stop_program",
    "resume": "button.dishwasher_resume_program",
    #: Seconds until a delayed start, counted from when the state changed to ``delayedstart``.
    "start_in": "number.dishwasher_start_in_relative",
}
DISHWASHER_END_TOLERANCE = 60
#: Progress moves this many points, at most this often, before a progress-only card is updated.
PROGRESS_STEP = 0.05
PROGRESS_INTERVAL = 300

# ---------------------------------------------------------------------------------------------------------------------
# Laundry: the started helpers turn on five minutes after the vibration starts (spec section 4)

LAUNDRY = {
    JOB_DRYER: "input_boolean.dryer_started_helper",
    JOB_WASHER: "input_boolean.washer_started_helper",
}
LAUNDRY_OFFSET = 300

# ---------------------------------------------------------------------------------------------------------------------
# Vacuums (spec sections 4 and 5)


@dataclass(frozen=True)
class Robot:
    job: str
    floor: str  # "Main Floor": used in "Cleaning Main Floor"
    vacuum: str  # the vacuum entity
    robot_id: str  # the Valetudo id in sensor.valetudo_<id>_status_flag and select.valetudo_<id>_mode
    #: ``sensor.<floor>_vacuum_status``: every robot's problem sensor, with the coordinator's ``current_issue`` contract.
    status_sensor: str
    #: ``sensor.*estimated_segment``: its state is the name of the room a manual segment run is in.
    segment_sensor: str
    #: The coordinator's object id (``main_floor_vacuum_coordinator``); only the Main Floor robot has one.
    coordinator: str | None = None

    @property
    def slug(self) -> str:
        return self.job.split(".", 1)[1]

    @property
    def status_flag(self) -> str:
        return f"sensor.valetudo_{self.robot_id}_status_flag"

    @property
    def error_sensor(self) -> str:
        """The robot's own error text: a last resort when ``status_sensor`` is missing."""
        return f"sensor.valetudo_{self.robot_id}_error"

    @property
    def mode_select(self) -> str:
        return f"select.valetudo_{self.robot_id}_mode"

    @property
    def fallback_helper(self) -> str:
        """Only the music room has one today; the others never exist, which reads as off."""
        return f"input_boolean.{self.slug}_vacuum_fallback_active"

    @property
    def error_message_helper(self) -> str:
        return f"input_text.{self.slug}_vacuum_error_message"


# Verified against the owner's Home Assistant on 2026-10-17 (planner review).
ROBOTS: dict[str, Robot] = {
    JOB_VAC_MAIN: Robot(
        job=JOB_VAC_MAIN, floor="Main Floor", vacuum="vacuum.valetudo_exaltedsneakydeer", robot_id="exaltedsneakydeer",
        status_sensor="sensor.main_floor_vacuum_status",
        segment_sensor="sensor.main_floor_robot_vacuum_estimated_segment",
        coordinator="main_floor_vacuum_coordinator"),
    JOB_VAC_MUSIC: Robot(
        job=JOB_VAC_MUSIC, floor="Music Room", vacuum="vacuum.valetudo_elatedusedram", robot_id="elatedusedram",
        status_sensor="sensor.music_room_vacuum_status",
        segment_sensor="sensor.music_room_music_room_robot_vacuum_estimated_segment"),
    JOB_VAC_THEATER: Robot(
        job=JOB_VAC_THEATER, floor="Theater Room", vacuum="vacuum.valetudo_politefatherlykingfisher",
        robot_id="politefatherlykingfisher", status_sensor="sensor.theater_room_vacuum_status",
        segment_sensor="sensor.theater_room_vacuum_estimated_segment_2"),
}

#: Coordinator room phases that mean a room is being cleaned right now.
COORDINATOR_ACTIVE_PHASES = frozenset({"cleaning", "resumed_cleaning"})
#: ``mode`` select option to the verb on the card.
MODE_VERBS = {"vacuum": "Vacuuming", "mop": "Mopping", "vacuum_and_mop": "Cleaning"}
#: ``current_issue.status`` values that mean the issue is over.
ISSUE_CLEARED = frozenset({"", "none", "clear", "cleared", "resolved", "ok", "closed"})
#: Vacuum states that end a manual run at once.
VACUUM_ENDED_STATES = frozenset({"returning", "docked", "idle", "error", "unavailable", "unknown"})


def coordinator_entities(robot: Robot) -> dict[str, str]:
    assert robot.coordinator
    return {
        "current_room": f"sensor.{robot.coordinator}_current_room",
        "auto_cleaning": f"binary_sensor.{robot.coordinator}_auto_cleaning",
    }


def last_cleaned_entity(robot: Robot, room_id: str) -> str:
    return f"sensor.{robot.coordinator}_{room_id}_last_cleaned"


#: Pause and Resume are plain ``vacuum.pause`` and ``vacuum.start`` (the coordinator watches the robot). ``set_paused``
#: pauses the automatic cleaning scheduler, not the robot, so it is not used. Dock while the coordinator runs a session
#: cancels it (the coordinator then sends the robot home).
COORDINATOR_DOMAIN = "valetudo_vacuum_coordinator"
COORDINATOR_CANCEL_SERVICE = "cancel_session"
COORDINATOR_CANCEL_REASON = "live_activity"

# ---------------------------------------------------------------------------------------------------------------------
# Hot flash (spec section 4a)

#: ``hot_flash_active`` phases: the bed is cooling or holding; ``restore_pending`` is the natural end.
HOT_FLASH_ACTIVE = frozenset({"cooling", "holding"})
HOT_FLASH_FINISHED = frozenset({"restore_pending"})
HOT_FLASH_HOLD_SECONDS = 900
#: The coldest level the bed chills to (Fahrenheit), for the cooling ring.
HOT_FLASH_TARGET_TEMP = 56.0


@dataclass(frozen=True)
class Side:
    key: str  # left / right
    owner: str  # person entity id
    owner_key: str  # the value ``requested_by`` takes when the owner started it
    phase: str
    timer: str
    restore_at: str
    requested_by: str
    cancel: str
    climate: str
    #: The hot flash card's tap target for this side alone (Whole bed uses the bedroom page).
    url: str = ""


SIDES: dict[str, Side] = {
    "left": Side("left", "person.stephen_fenton", "stephen", "input_select.sleepypod_hot_flash_left_phase",
                 "timer.sleepypod_hot_flash_left", "input_datetime.sleepypod_hot_flash_left_restore_at",
                 "input_text.eight_sleep_stephen_hot_flash_requested_by",
                 "input_button.eight_sleep_stephen_cancel_hot_flash", "climate.sleepypod_eight_pod_left_side", DASH + "master-bedroom#stephens-bed"),
    "right": Side("right", "person.stephanie_hobart", "steph", "input_select.sleepypod_hot_flash_right_phase",
                  "timer.sleepypod_hot_flash_right", "input_datetime.sleepypod_hot_flash_right_restore_at",
                  "input_text.eight_sleep_steph_hot_flash_requested_by",
                  "input_button.eight_sleep_steph_cancel_hot_flash", "climate.sleepypod_eight_pod_right_side", DASH + "master-bedroom#stephs-bed"),
}
#: ``requested_by`` value to the person it names, and the name shown on the card.
REQUESTERS = {"stephen": "person.stephen_fenton", "steph": "person.stephanie_hobart"}
REQUESTER_NAMES = {"stephen": "Stephen", "steph": "Steph"}
UNATTRIBUTED = "unattributed"
AWAY_STATUS = "sensor.household_away_status"
VACATION_MODE = "binary_sensor.effective_vacation_mode"
#: ``sensor.household_away_status``: ``mode`` is ``solo_trip`` while a trip is on, and ``traveler`` is ``stephen``,
#: ``steph`` or ``none``.
AWAY_TRAVELLER_ATTRIBUTES = ("traveler",)

# ---------------------------------------------------------------------------------------------------------------------
# Donetick (spec section 5)

TODO_ENTITY = "todo.all_tasks_internal"
#: HA user id to Donetick user id, for ``donetick.complete_task``'s ``completed_by``: Stephen is 1, Steph is 2.
DONETICK_USERS: dict[str, int] = {
    "64089b5683944c39b4f944c8f76830b0": 1,
    "43cb71bbd1cb4860b2a7de4c829020f0": 2,
}

# ---------------------------------------------------------------------------------------------------------------------
# Household notification fallback (spec section 5)

#: The notify action that reaches a person's whole phone (Companion included): ``notify.stephen_s_phone``, where ``*``
#: stands for the person's first name.
DEFAULT_PERSON_NOTIFY = "*_s_phone"


def style_for(job_id: str) -> Style:
    return STYLES.get(job_id) or Style(job_id, "problem", "alert", "neutral", "/sfenton-react-dash/home")


def resolve_device(device: str) -> str | None:
    """A ``fenton_home.problem`` ``device`` to a job id: ``main_floor``, ``vacuum.main_floor`` or ``dishwasher``."""
    name = (device or "").strip().lower().replace(" ", "_").replace("-", "_")
    if name.startswith(TAG_PREFIX):
        name = name[len(TAG_PREFIX):]
    if name in STYLES:
        return name
    if f"vacuum.{name}" in STYLES:
        return f"vacuum.{name}"
    for suffix in ("_vacuum", "_robot"):
        if name.endswith(suffix) and f"vacuum.{name[:-len(suffix)]}" in STYLES:
            return f"vacuum.{name[:-len(suffix)]}"
    if f"fan.{name}" in STYLES:
        return f"fan.{name}"
    return None


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()
