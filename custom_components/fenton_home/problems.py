"""Problem cards (spec section 5): the code table, the wording, the raw-text fallbacks and the problem records.

Wording lives here so that new wording ships with a relay update and never needs an app release. Everything in this
module is pure (no Home Assistant): ``build_problem`` turns a code into the card's ``ProblemInfo`` and
``ProblemRecord`` is the engine's bookkeeping for one open problem (its Donetick task and the fallback push).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import tzinfo

from .model import Control, ProblemInfo

CRITICAL = "critical"
WARNING = "warning"

#: Glyph ids the app draws for a problem (spec section 5).
ICONS = frozenset({"alert", "door", "dock", "edge", "brush", "battery", "water", "waste", "mop", "bag", "question",
                   "door_dish"})

MAX_RAW = 60
MAX_MESSAGE = 90
UNKNOWN_PROBLEM = "Stopped with an error"


@dataclass(frozen=True)
class Row:
    """One line of the section 5 table."""

    code: str
    severity: str
    icon: str
    problem: str
    message: str
    #: Button ids in order (``controls.PROBLEM_BUTTON_TITLES``); Done is appended when a task is linked.
    buttons: tuple[str, ...] = ()
    #: Shown when the room is unknown.
    problem_noroom: str | None = None
    message_noroom: str | None = None
    #: Whether ``script.valetudo_error_mapping`` creates a Donetick task for it (informational; Done needs a linked task).
    taskable: bool = True


_RESUME_DOCK = ("resume", "dock")

ROWS = (
    Row("navigation.stuck", CRITICAL, "alert", "Stuck in {room}", "Move it somewhere flat, then tap Resume.",
        _RESUME_DOCK, problem_noroom="Stuck"),
    Row("navigation.target_unreachable", CRITICAL, "door", "Can't reach {room}",
        "A door may be closed. Open it, then tap Resume.", _RESUME_DOCK, problem_noroom="Can't reach its room"),
    Row("navigation.dock_unreachable", CRITICAL, "dock", "Can't get back to the dock",
        "Clear the way to the dock or carry it there.", ("try_again",)),
    Row("navigation.ramp_hazard", CRITICAL, "edge", "Stopped at a drop or ramp",
        "Move it away from the edge, then tap Resume.", _RESUME_DOCK),
    Row("maintenance.main_brush_jammed", CRITICAL, "brush", "Main brush jammed",
        "Turn it over, clear the brush, then tap Resume.", _RESUME_DOCK),
    Row("maintenance.side_brush_jammed", CRITICAL, "brush", "Side brush jammed",
        "Untangle the side brush, then tap Resume.", _RESUME_DOCK),
    Row("power.critical_battery", CRITICAL, "battery", "Battery critically low", "Carry it to the dock to charge.",
        taskable=False),
    Row("power.low_battery", WARNING, "battery", "Battery low", "Heading to the dock. It finishes {room} after charging.",
        message_noroom="Heading to the dock. It finishes after charging.", taskable=False),
    Row("mop.clean_water_empty", WARNING, "water", "Clean water tank empty",
        "Refill the clean water tank on the dock, then tap Resume.", ("resume",)),
    Row("mop.dirty_water_unavailable", WARNING, "waste", "Wastewater tank full or missing",
        "Empty the wastewater tank and seat it firmly.", ("resume",)),
    Row("mop.attachment_missing", WARNING, "mop", "Mop pads missing", "Attach the mop pads, or it vacuums only.",
        ("vacuum_only",)),
    Row("mop.vacuum_only_fallback", WARNING, "mop", "Vacuuming only",
        "Mopping isn't available. Check the mop attachment, water tanks and pads.", taskable=False),
    Row("dock.cover_open", WARNING, "bag", "Dock cover open", "Check the dust bag is in, then close the cover."),
    Row("dock.dustbag_blocked", WARNING, "bag", "Dust bag full", "Replace the dust bag in the dock."),
    Row("dishwasher.action_required", WARNING, "door_dish", "Needs attention", "Check the dishwasher's display.",
        taskable=False),
    Row("dishwasher.error", CRITICAL, "question", UNKNOWN_PROBLEM, "Check the dishwasher's display.", taskable=False),
    # Not in the section 5 table: the dishwasher's "Refill rinse aid" Donetick task, which section 5 says the same
    # service takes later. It has no auto-complete, so only Done or Donetick ends it.
    Row("dishwasher.rinse_aid", WARNING, "water", "Refill rinse aid", "Add rinse aid to the dishwasher."),
)
TABLE = {row.code: row for row in ROWS}

#: Vendor error numbers that have their own row (spec section 5).
NUMBERED = {75: "power.critical_battery", 95: "navigation.ramp_hazard", 120: "mop.attachment_missing"}

_UNKNOWN_N = re.compile(r"unknown\s+error\s+(\d+)", re.IGNORECASE)
_VENDOR_CODE = re.compile(r"^vendor\.unknown_error_(\d+)$")
_NO_ERROR = frozenset({"", "no error", "none", "ok", "unknown", "unavailable", "no_error"})

#: Best-effort keywords for raw Valetudo text with no coordinator mapping. The automation passes ``code`` from its own
#: mapping (``issue_code_for_raw``), which always wins; this is only for robots with no coordinator.
_KEYWORDS = (
    ("main brush", "maintenance.main_brush_jammed"),
    ("side brush", "maintenance.side_brush_jammed"),
    ("dust bag", "dock.dustbag_blocked"),
    ("dustbag", "dock.dustbag_blocked"),
    ("cover", "dock.cover_open"),
    ("clean water", "mop.clean_water_empty"),
    ("water tank empty", "mop.clean_water_empty"),
    ("dirty water", "mop.dirty_water_unavailable"),
    ("wastewater", "mop.dirty_water_unavailable"),
    ("mop pad", "mop.attachment_missing"),
    ("mop attach", "mop.attachment_missing"),
    ("low battery", "power.low_battery"),
    ("return to dock", "navigation.dock_unreachable"),
    ("reach the dock", "navigation.dock_unreachable"),
    ("stuck", "navigation.stuck"),
)


def slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")[:40] or "error"


def code_for_raw(raw: str | None) -> str | None:
    """The code for a robot's error text: the known table, then ``unknown error N``, else ``unmapped.<slug>``.

    ``None`` when the text means there is no error.
    """
    text = (raw or "").strip()
    if text.lower() in _NO_ERROR:
        return None
    if match := _UNKNOWN_N.search(text):
        number = int(match.group(1))
        return NUMBERED.get(number, f"vendor.unknown_error_{number}")
    lowered = text.lower()
    for keyword, code in _KEYWORDS:
        if keyword in lowered:
            return code
    return f"unmapped.{slug(text)}"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def quote_message(raw: str) -> str:
    """The unmapped card's instruction, quoting the robot's text (60 characters at most, 90 for the sentence)."""
    raw = _clip(" ".join(raw.split()), MAX_RAW)
    while True:
        message = f'The robot says "{raw}". Check it, then tap Resume.'
        if len(message) <= MAX_MESSAGE or len(raw) <= 8:
            return message
        raw = _clip(raw, len(raw) - 4)


def _fill(template: str, room: str | None, without_room: str | None) -> str:
    """``template`` with its ``{room}``, or the wording for an unknown room."""
    if "{room}" not in template:
        return template
    if room:
        return template.format(room=room)
    return without_room if without_room is not None else template.replace(" {room}", "")


def kind_of(code: str) -> str:
    """``table``, ``numbered`` (vendor.unknown_error_N) or ``unmapped``."""
    if code in TABLE:
        return "table"
    if _VENDOR_CODE.match(code):
        return "numbered"
    return "unmapped"


def build_problem(
    code: str,
    raw: str | None,
    *,
    room: str | None,
    since: float | None,
    tz: tzinfo | None,
    make_button: Callable[[str], Control | None],
    task_id: int | None = None,
    make_done: Callable[[int], Control] | None = None,
    title: str | None = None,
    message: str | None = None,
) -> ProblemInfo:
    """The card's problem for a code.

    ``make_button`` turns a button id into the control for this device (None: the device has no such control).
    Done is appended last when a Donetick task is linked (``task_id``); a problem without a task never has it.
    ``title`` and ``message`` are the automation's own wording, used only for a service-raised code the table does not
    know and that is not an unknown or unmapped robot error.
    """
    kind = kind_of(code)
    chips: tuple[str, ...]
    if kind == "table":
        row = TABLE[code]
        problem = _fill(row.problem, room, row.problem_noroom)
        text = _fill(row.message, room, row.message_noroom)
        severity, icon, buttons = row.severity, row.icon, row.buttons
        chips = ()
    elif kind == "numbered":
        number = _VENDOR_CODE.match(code).group(1)  # type: ignore[union-attr]
        problem, text = UNKNOWN_PROBLEM, "Check the robot, then tap Resume."
        severity, icon, buttons = CRITICAL, "question", _RESUME_DOCK
        chips = (f"Error {number}",)
    else:
        if raw:
            problem, text = UNKNOWN_PROBLEM, quote_message(raw)
        elif title:
            problem, text = _clip(title, 40), _clip(message or "Check the device.", MAX_MESSAGE)
        else:
            problem, text = UNKNOWN_PROBLEM, "Check the device, then tap Resume."
        severity, icon, buttons = CRITICAL, "question", _RESUME_DOCK if raw or not title else ()
        chips = ()
    controls = [c for b in buttons if (c := make_button(b)) is not None]
    if task_id is not None and make_done is not None:
        controls.append(make_done(task_id))
    return ProblemInfo(severity=severity, code=code, icon=icon, problem=_clip(problem, 40), message=_clip(text, MAX_MESSAGE),
                       chips=chips, task_id=task_id, controls=tuple(controls), since=since)


# ---------------------------------------------------------------------------------------------------------------------
# Records


@dataclass
class ProblemRecord:
    """An open problem on one device (one record per job id).

    Raised by a sensor (``source == "sensor"``) or by the ``fenton_home.problem`` service (``"service"``). A service
    record carries the automation's wording, the push tag of the fallback notification and the Donetick task.
    """

    job_id: str
    code: str
    raw: str | None
    source: str
    created: float
    room: str | None = None
    since: float | None = None
    #: The sensor path has reported this problem at least once.
    sensor_seen: bool = False
    #: The device was seen not running after the sensors reported this problem. Stored with the record; a record
    #: stored before this field existed reads False, so its card waits for its task.
    seen_idle: bool = False
    task_title: str | None = None
    task_id: int | None = None
    lookups: int = 0
    last_lookup: float = 0.0
    url: str | None = None
    push_tag: str | None = None
    #: People who got the fallback push (and so get the clearing push), as person entity ids.
    pushed_to: list[str] = field(default_factory=list)
    title: str | None = None
    message: str | None = None
    #: Set once the linked task is seen completed.
    task_completed: bool = False

    @property
    def taskable(self) -> bool:
        return self.task_title is not None

    def awaiting_task(self, now: float) -> bool:
        """Whether the Donetick task is still being looked for (it may not have been created yet)."""
        return self.taskable and self.task_id is None and self.lookups < MAX_LOOKUPS \
            and now - self.created < LOOKUP_WINDOW


#: Donetick lookups per record before it is treated as having no task, and the longest to keep looking (seconds).
MAX_LOOKUPS = 5
LOOKUP_WINDOW = 60.0
