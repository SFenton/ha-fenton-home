"""Job adapters (spec sections 4 and 4a): pure functions from entity states to the cards the engine shows.

``derive`` returns one ``JobState`` per running job (hot flash returns one per person who sees it); ``signals``
returns the problems the sensors currently report. Neither touches Home Assistant, so both are unit-tested on
recorded state snapshots (``States``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from . import controls as ctl
from .entities import (
    AWAY_STATUS,
    AWAY_TRAVELLER_ATTRIBUTES,
    COORDINATOR_ACTIVE_PHASES,
    DISHWASHER,
    DISHWASHER_END_TOLERANCE,
    FAN_END_TOLERANCE,
    FAN_ROOMS,
    HOT_FLASH_ACTIVE,
    HOT_FLASH_FINISHED,
    HOT_FLASH_HOLD_SECONDS,
    HOT_FLASH_TARGET_TEMP,
    ISSUE_CLEARED,
    JOB_DISHWASHER,
    JOB_HOT_FLASH,
    LAUNDRY,
    LAUNDRY_OFFSET,
    MODE_VERBS,
    REQUESTER_NAMES,
    REQUESTERS,
    ROBOTS,
    SIDES,
    UNATTRIBUTED,
    VACATION_MODE,
    VACUUM_ENDED_STATES,
    Robot,
    Side,
    clean,
    coordinator_entities,
    fan_entities,
    last_cleaned_entity,
    style_for,
)
from .model import (
    JobState,
    PersonRef,
    Tolerance,
    countdown,
    countup,
    percent,
    text,
)
from .problems import code_for_raw
from .snapshot import States, parse_time

COMPLETE = "Complete"


@dataclass
class Memory:
    """What the adapters need to remember between snapshots. The engine owns and updates it."""

    #: Hot flash side key to who started it (``stephen``, ``steph`` or ``unattributed``), captured when it began.
    hot_flash_starter: dict[str, str] = field(default_factory=dict)
    #: Hot flash side key to the bed temperature when cooling began.
    hot_flash_start_temp: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Ctx:
    """The non-state inputs of the adapters."""

    tz: tzinfo | None = None
    people: tuple[PersonRef, ...] = ()
    memory: Memory = field(default_factory=Memory)
    enabled: frozenset[str] | None = None  # None: every job

    def on(self, job_id: str) -> bool:
        return self.enabled is None or job_id in self.enabled


@dataclass(frozen=True)
class ProblemSignal:
    """A problem a sensor reports right now."""

    job_id: str
    code: str
    raw: str | None = None
    room: str | None = None
    since: float | None = None


def relevance_countdown(end_ts: float, now: float) -> float:
    """``80 - min(minutes left, 60) / 2``: sooner is higher, 50 to 80 (spec section 6)."""
    minutes = max(0.0, (end_ts - now) / 60)
    return 80.0 - min(minutes, 60.0) / 2


def _card(job_id: str, phase: str, value: dict, **fields) -> JobState:
    style = style_for(job_id)
    fields.setdefault("url", style.url)
    return JobState(job_id=job_id, title=style.title, kind=style.kind, symbol=style.symbol, tint=style.tint,
                    phase=phase, value=value, **fields)


def complete_card(job_id: str, *, audience: frozenset[str] | None = None, headline: str | None = None,
                  detail: str | None = None, url: str | None = None, linger_holds: bool = False) -> JobState:
    """The card of a job that finished on its own: a filled ring and "Complete" (relevance 10)."""
    return _card(job_id, "done", text(COMPLETE), headline=headline, detail=detail, progress=1.0, complete=True,
                 relevance=10.0, url=url or style_for(job_id).url,
                 linger_holds=linger_holds, audience=audience, open_ended=False)


def as_complete(job: JobState) -> JobState:
    """The Complete card for a running laundry job that has just finished (it keeps its audience and headline)."""
    return complete_card(job.job_id, audience=job.audience, headline=job.headline, detail=job.detail,
                         url=job.url, linger_holds=True)


# -- Fans -------------------------------------------------------------------------------------------------------------


def fan(job_id: str, states: States, now: float, ctx: Ctx) -> JobState | None:
    """Running while ``timer_pending`` is on and ``ends_at`` is in the future (``ends_at`` goes stale after a cancel)."""
    room = FAN_ROOMS[job_id]
    entities = fan_entities(room)
    if not states.is_on(entities["pending"]):
        return None
    ends = parse_time(states.attr(entities["ends_at"], "timestamp"))
    if ends is None:
        ends = states.timestamp(entities["ends_at"], ctx.tz)
    if ends is None or ends <= now:
        return None
    return _card(job_id, "running", countdown(ends, states.changed(entities["pending"])),
                 controls=(ctl.fan_stop(room),), relevance=relevance_countdown(ends, now),
                 tolerance=Tolerance(end_seconds=FAN_END_TOLERANCE), open_ended=False)


# -- Dishwasher -------------------------------------------------------------------------------------------------------


def _clock(moment: float, tz: tzinfo | None) -> str:
    local = datetime.fromtimestamp(moment, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _delayed_start(states: States) -> float | None:
    """When a delayed start begins: the moment the state changed plus ``start_in_relative`` seconds (when above zero)."""
    changed, relative = states.changed(DISHWASHER["state"]), states.number(DISHWASHER["start_in"])
    return changed + relative if changed and relative and relative > 0 else None


def dishwasher(states: States, now: float, ctx: Ctx) -> JobState | None:
    op = (states.known(DISHWASHER["state"]) or "").lower()
    remote = states.is_on(DISHWASHER["remote"])
    if op == "finished":
        return complete_card(JOB_DISHWASHER)
    if op not in ("run", "pause", "delayedstart"):
        return None
    tolerance = Tolerance(end_seconds=DISHWASHER_END_TOLERANCE)
    if op == "delayedstart":
        start = _delayed_start(states)
        label = f"Starts {_clock(start, ctx.tz)}" if start and start > now else "Delayed start"
        return _card(JOB_DISHWASHER, "waiting", text(label), relevance=55.0, tolerance=tolerance,
                     controls=(ctl.dishwasher_stop(),) if remote else ())
    finish = states.timestamp(DISHWASHER["finish"], ctx.tz)
    progress = states.number(DISHWASHER["progress"])
    fraction = None if progress is None else min(max(progress / 100.0, 0.0), 1.0)
    if op == "pause":
        stop = (ctl.dishwasher_resume(), ctl.dishwasher_stop()) if remote else ()
        return _card(JOB_DISHWASHER, "paused", text("Paused"), progress=fraction, controls=stop, relevance=55.0,
                     tolerance=tolerance)
    controls = (ctl.dishwasher_stop(),) if remote else ()
    if finish is not None and finish > now:
        return _card(JOB_DISHWASHER, "running", countdown(finish, states.changed(DISHWASHER["state"])),
                     controls=controls, relevance=relevance_countdown(finish, now), tolerance=tolerance,
                     open_ended=False)
    if fraction is not None:
        return _card(JOB_DISHWASHER, "running", percent(), progress=fraction, controls=controls, relevance=55.0,
                     tolerance=tolerance)
    return _card(JOB_DISHWASHER, "running", text("Running"), controls=controls, relevance=55.0, tolerance=tolerance)


# -- Laundry ----------------------------------------------------------------------------------------------------------


def laundry(job_id: str, states: States, now: float, ctx: Ctx) -> JobState | None:
    """Count-up from five minutes before the helper turned on. No estimates, no buttons (owner decisions 7 and 9)."""
    helper = LAUNDRY[job_id]
    if not states.is_on(helper):
        return None
    start = (states.changed(helper) or now) - LAUNDRY_OFFSET
    return _card(job_id, "running", countup(start), relevance=55.0, complete_on_drop=True)


# -- Vacuums ----------------------------------------------------------------------------------------------------------


def _title_case(room_id: str) -> str:
    return room_id.replace("_", " ").strip().title()


def room_label(robot: Robot, room_id: str, states: States) -> str:
    """The room's name from ``sensor.<coordinator>_<room>_last_cleaned``, else the id in title case."""
    entity = last_cleaned_entity(robot, room_id)
    for key in ("room_name", "room", "name"):
        if name := clean(states.attr(entity, key)):
            return name
    return _title_case(room_id)


def _current_room(robot: Robot, states: States) -> tuple[str, str]:
    entities = coordinator_entities(robot)
    return (clean(states.attr(entities["current_room"], "room_id")),
            clean(states.attr(entities["current_room"], "phase")).lower())


def _verb(robot: Robot, states: States) -> str:
    entities = coordinator_entities(robot)
    if states.attr(entities["current_room"], "vacuum_only") in (True, "true", "True", "on"):
        return "Vacuuming"
    mode = (states.known(robot.mode_select) if robot.mode_select else None) or ""
    return MODE_VERBS.get(mode.lower(), "Cleaning")


def _session(robot: Robot, states: States) -> bool:
    """Whether the coordinator is running an automatic cleaning session on this robot."""
    return bool(robot.coordinator) and states.is_on(coordinator_entities(robot)["auto_cleaning"])


def vacuum(robot: Robot, states: States, now: float, ctx: Ctx) -> JobState | None:
    vac = states.state(robot.vacuum).lower()
    if vac in VACUUM_ENDED_STATES:
        return None
    if robot.coordinator:
        room_id, phase = _current_room(robot, states)
        if room_id and (phase in COORDINATOR_ACTIVE_PHASES or vac == "paused"):
            return _coordinator_room(robot, room_id, vac, states)
        if _session(robot, states):
            return None  # between rooms of a session
    if vac in ("cleaning", "paused"):
        return _manual_run(robot, vac, states)
    return None


MAX_SHORT = 10


def _running_card(robot: Robot, headline: str, paused: str, short: str, session_key: str, vac: str, states: States, *,
                  session: bool) -> JobState:
    """A vacuum card: no time, no ring (owner, 2026-10-17). ``headline`` is the line; ``short`` fits the compact island."""
    if vac == "paused":
        return _card(robot.job, "paused", None, headline=paused, short="Paused",
                     controls=(ctl.vacuum_resume(robot, session), ctl.vacuum_dock(robot, session)),
                     relevance=55.0, session_key=session_key)
    return _card(robot.job, "running", None, headline=headline, short=short[:MAX_SHORT],
                 controls=(ctl.vacuum_pause(robot, session), ctl.vacuum_dock(robot, session)),
                 relevance=55.0, session_key=session_key)


def _coordinator_room(robot: Robot, room_id: str, vac: str, states: States) -> JobState:
    room = room_label(robot, room_id, states)
    return _running_card(robot, f"{_verb(robot, states)} {room}", f"Paused in {room}", room, f"room:{room_id}", vac,
                         states, session=_session(robot, states))


def _manual_run(robot: Robot, vac: str, states: States) -> JobState:
    """A run nobody scheduled: the status flag (state only) says what kind, the segment sensor which room."""
    flag = (states.known(robot.status_flag) or "none").lower()
    segment = clean(states.known(robot.segment_sensor)) if flag == "segment" else ""
    if flag == "segment":
        headline, short = f"Vacuuming {segment}".strip(), segment or "Cleaning"
    elif flag == "zone":
        headline, short = f"Cleaning {robot.floor} area", "Area"
    elif flag == "spot":
        headline, short = "Spot cleaning", "Spot"
    else:
        headline, short = f"Cleaning {robot.floor}", "Cleaning"
    paused = f"Paused in {segment}" if segment else "Paused"
    return _running_card(robot, headline, paused, short, f"{flag}:{segment}", vac, states, session=False)


def _issue(robot: Robot, states: States) -> tuple[str, str | None, float | None] | None:
    """(code, raw, when) of the status sensor's active ``current_issue``; None when it is clear.

    The sensor's contract: ``{status: "clear"}`` when nothing is wrong, else ``{code, raw, reported_at, ...}``.
    """
    issue = states.attr(robot.status_sensor, "current_issue")
    if not isinstance(issue, dict) or clean(issue.get("status")).lower() in ISSUE_CLEARED - {""}:
        return None
    code, raw = clean(issue.get("code")), clean(issue.get("raw")) or None
    if not code:
        code = code_for_raw(raw) or ""
    if not code:
        return None
    return code, raw, parse_time(issue.get("reported_at") or issue.get("since"))


def vacuum_signals(robot: Robot, states: States, now: float) -> ProblemSignal | None:
    """The problem a robot reports: its status sensor's ``current_issue``; the robot's own error text only when the
    status sensor is missing; then the vacuum-only fallback helper."""
    room = None
    if robot.coordinator:
        room_id, _ = _current_room(robot, states)
        room = room_label(robot, room_id, states) if room_id else None
    if states.known(robot.status_sensor) is not None:
        if issue := _issue(robot, states):
            code, raw, when = issue
            return ProblemSignal(robot.job, code, raw, room, when)
    else:
        raw = states.known(robot.error_sensor)
        if code := code_for_raw(raw):
            return ProblemSignal(robot.job, code, raw, room, states.changed(robot.error_sensor))
    if states.is_on(robot.fallback_helper):
        return ProblemSignal(robot.job, "mop.vacuum_only_fallback",
                             clean(states.state(robot.error_message_helper, "")) or None, room,
                             states.changed(robot.fallback_helper))
    return None


def dishwasher_signal(states: States) -> ProblemSignal | None:
    op = (states.known(DISHWASHER["state"]) or "").lower()
    since = states.changed(DISHWASHER["state"])
    if op == "actionrequired":
        return ProblemSignal(JOB_DISHWASHER, "dishwasher.action_required", None, None, since)
    if op == "error":
        return ProblemSignal(JOB_DISHWASHER, "dishwasher.error", None, None, since)
    return None


# -- Hot flash (spec section 4a) --------------------------------------------------------------------------------------


def _resolve_people(values: Iterable[object], people: Iterable[PersonRef]) -> set[str]:
    """Person entity ids named by a loose attribute value: an entity id, a first name or an object id."""
    known = list(people)
    found: set[str] = set()
    for value in values:
        token = clean(value).lower()
        if not token:
            continue
        for person in known:
            if token in (person.entity_id.lower(), person.entity_id.split(".", 1)[-1].lower(), person.first.lower(),
                         person.name.lower(), person.user_id.lower()):
                found.add(person.entity_id)
        for key, entity in REQUESTERS.items():
            if token in (key, entity.lower()):
                found.add(entity)
    return found


def travellers(states: States, ctx: Ctx) -> set[str]:
    """The person on a Solo Trip, or nobody."""
    if clean(states.attr(AWAY_STATUS, "mode")).lower() != "solo_trip":
        return set()
    values: list[object] = []
    for key in AWAY_TRAVELLER_ATTRIBUTES:
        value = states.attr(AWAY_STATUS, key)
        if isinstance(value, (list, tuple, set)):
            values.extend(value)
        elif value is not None:
            values.append(value)
    return _resolve_people([v for v in values if clean(v).lower() != "none"], ctx.people)


def hot_flash_audience(side: Side, starter: str, away: set[str], vacation: bool) -> frozenset[str]:
    """Who sees one side's hot flash (spec section 4a).

    The side's owner and the person who asked for it; the owner alone when an automation did. A Solo Trip removes the
    traveller, even from their own side. Vacation removes everyone except the person who started it.
    """
    starter_person = REQUESTERS.get(starter)
    members = {side.owner}
    if starter_person:
        members.add(starter_person)
    if vacation:
        members = {starter_person} if starter_person else set()
    return frozenset(members - away)


def _phase(states: States, side: Side) -> str:
    return (states.known(side.phase) or "").lower()


def note_hot_flash(memory: Memory, states: States) -> None:
    """Remember who started each side, and how warm the bed was, the first time the side is seen running or finishing.

    ``requested_by`` is stale between runs, so it is read once per run and never again: a later change to the helper
    while the run goes on changes nothing.
    """
    for key, side in SIDES.items():
        phase = _phase(states, side)
        if phase in HOT_FLASH_ACTIVE or phase in HOT_FLASH_FINISHED:
            if key not in memory.hot_flash_starter:
                requested = clean(states.state(side.requested_by, "")).lower()
                memory.hot_flash_starter[key] = requested if requested in REQUESTERS else UNATTRIBUTED
            if phase in HOT_FLASH_ACTIVE and key not in memory.hot_flash_start_temp \
                    and (temp := _temperature(states, side)) is not None:
                memory.hot_flash_start_temp[key] = temp
        else:
            memory.hot_flash_starter.pop(key, None)
            memory.hot_flash_start_temp.pop(key, None)


def _temperature(states: States, side: Side) -> float | None:
    value = states.attr(side.climate, "current_temperature")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _cooling_progress(keys: list[str], states: States, memory: Memory) -> float | None:
    parts = []
    for key in keys:
        start, now_temp = memory.hot_flash_start_temp.get(key), _temperature(states, SIDES[key])
        if start is None or now_temp is None or start <= HOT_FLASH_TARGET_TEMP:
            continue
        parts.append(min(max((start - now_temp) / (start - HOT_FLASH_TARGET_TEMP), 0.0), 1.0))
    return round(sum(parts) / len(parts), 3) if parts else None


def _hold_end(states: States, side: Side, ctx: Ctx) -> float | None:
    end = parse_time(states.attr(side.timer, "finishes_at"), ctx.tz)
    if end is None:
        end = parse_time(states.attr(side.restore_at, "timestamp"))
    return end


MAX_DETAIL = 32


def _detail(person: str, keys: list[str], memory: Memory) -> str:
    """"Your side", "Stephen's side" or "Whole bed", plus " · by Steph" (or " · by Home") when someone else started it
    and it fits in 32 characters."""
    if len(keys) == 2:
        label = "Whole bed"
    else:
        side = SIDES[keys[0]]
        label = "Your side" if side.owner == person else f"{REQUESTER_NAMES.get(side.owner_key, side.owner_key)}'s side"
    viewer = next((k for k, entity in REQUESTERS.items() if entity == person), None)
    others = [s for s in (memory.hot_flash_starter.get(k, UNATTRIBUTED) for k in keys) if s != viewer]
    if others:
        by = others[0]
        suffix = f" · by {'Home' if by == UNATTRIBUTED else REQUESTER_NAMES.get(by, by.title())}"
        if len(label + suffix) <= MAX_DETAIL:
            label += suffix
    return label[:MAX_DETAIL]


def hot_flash(states: States, now: float, ctx: Ctx) -> list[JobState]:
    """One card per person who sees the hot flash (the audience rules decide who)."""
    memory = ctx.memory
    phases = {key: _phase(states, side) for key, side in SIDES.items()}
    live = {key: p for key, p in phases.items() if p in HOT_FLASH_ACTIVE or p in HOT_FLASH_FINISHED}
    if not live:
        return []
    away, vacation = travellers(states, ctx), states.is_on(VACATION_MODE)
    members = {key: hot_flash_audience(SIDES[key], memory.hot_flash_starter.get(key, UNATTRIBUTED), away, vacation)
               for key in live}
    cards: list[JobState] = []
    for person in sorted(set().union(*members.values())):
        view = [key for key in live if person in members[key]]
        active = [key for key in view if live[key] in HOT_FLASH_ACTIVE]
        shown = active or view
        detail = _detail(person, shown, memory)
        audience = frozenset({person})
        url = SIDES[shown[0]].url if len(shown) == 1 else None
        if not active:
            cards.append(complete_card(JOB_HOT_FLASH, audience=audience, detail=detail, url=url, linger_holds=True))
            continue
        if any(live[key] == "cooling" for key in active):
            value, progress = text("Cooling"), _cooling_progress([k for k in active if live[k] == "cooling"], states, memory)
        else:
            ends = [e for e in (_hold_end(states, SIDES[k], ctx) for k in active) if e is not None]
            end = max(ends) if ends else None
            value = countdown(end, end - HOT_FLASH_HOLD_SECONDS) if end else text("Holding")
            progress = None
        cards.append(_card(JOB_HOT_FLASH, "running", value, detail=detail, url=url or style_for(JOB_HOT_FLASH).url, progress=progress,
                           controls=(ctl.hot_flash_stop(tuple(active)),), relevance=70.0, audience=audience,
                           open_ended=False if value["kind"] == "countdown" else True))
    return cards


# -- Everything -------------------------------------------------------------------------------------------------------


def derive(states: States, now: float, ctx: Ctx) -> list[JobState]:
    """Every card the household's jobs call for right now (problems are overlaid by the engine)."""
    jobs: list[JobState] = []
    for job_id in FAN_ROOMS:
        if ctx.on(job_id) and (card := fan(job_id, states, now, ctx)):
            jobs.append(card)
    if ctx.on(JOB_DISHWASHER) and (card := dishwasher(states, now, ctx)):
        jobs.append(card)
    for job_id in LAUNDRY:
        if ctx.on(job_id) and (card := laundry(job_id, states, now, ctx)):
            jobs.append(card)
    for robot in ROBOTS.values():
        if ctx.on(robot.job) and (card := vacuum(robot, states, now, ctx)):
            jobs.append(card)
    if ctx.on(JOB_HOT_FLASH):
        jobs.extend(hot_flash(states, now, ctx))
    return jobs


def signals(states: States, now: float, ctx: Ctx) -> list[ProblemSignal]:
    """Every problem the sensors report right now, at most one per device."""
    found: list[ProblemSignal] = []
    for robot in ROBOTS.values():
        if ctx.on(robot.job) and (signal := vacuum_signals(robot, states, now)):
            found.append(signal)
    if ctx.on(JOB_DISHWASHER) and (signal := dishwasher_signal(states)):
        found.append(signal)
    return found
