"""The job engine's brain (spec sections 2 to 7), with no Home Assistant in it.

``Engine.refresh`` turns a snapshot of entity states into the cards that should exist (jobs, Complete lingers, problem
overlays). ``Engine.plan`` turns those cards into the pushes each device needs: starts, updates and ends, after slots,
relevance, coalescing, dismissals and the 7 h 45 min restart. ``live_jobs.LiveJobs`` runs both against Home Assistant
and sends what ``plan`` returns, so every rule here is unit-tested without Home Assistant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from . import controls as ctl
from .entities import (
    DISMISS_ECHO_SECONDS,
    HEARTBEAT_SECONDS,
    LINGER_SECONDS,
    MAX_AGE_SECONDS,
    OPEN_ENDED_STALE_SECONDS,
    PROBLEM_GRACE_SECONDS,
    ROBOTS,
    coordinator_entities,
    style_for,
)
from .jobs import Ctx, Memory, as_complete, derive, note_hot_flash, signals
from .model import JobState, ProblemInfo, text
from .problems import ProblemRecord, build_problem, kind_of
from .slots import Candidate, Coalescer, allocate
from .snapshot import States

_LOGGER = logging.getLogger(__name__)

STALE_GRACE = 600
#: A notice that has not found its moment to be sent (coalescing) is dropped after this long.
NOTICE_SECONDS = 30.0
CRITICAL_SCORE = 100.0
WARNING_SCORE = 90.0


@dataclass(frozen=True)
class Registration:
    """A device that can show cards: a Fenton Home registration with a push-to-start token."""

    webhook_id: str
    user_id: str
    name: str = ""
    person: str | None = None  # the person entity id, when the user is linked to one


@dataclass
class Card:
    """A card a device is showing (as far as the engine knows)."""

    started_at: float
    last_push: float
    content: dict[str, Any]
    session_key: str | None = None
    critical_sent: bool = False
    last_progress: float | None = None
    last_progress_push: float = 0.0
    relevance: float = 0.0


@dataclass(frozen=True)
class Push:
    """Send this card to this device (core chooses start or update from the tokens it holds)."""

    webhook_id: str
    tag: str
    job: JobState
    content: dict[str, Any]
    relevance: float
    stale_ts: int
    urgent: bool = False
    alert: bool = False
    first: bool = False  # the engine believes this is a start


@dataclass(frozen=True)
class End:
    """Remove this card from this device now."""

    webhook_id: str
    tag: str
    reason: str


@dataclass
class Plan:
    actions: list[Push | End] = field(default_factory=list)
    #: Seconds until the plan should be recomputed (a coalescing window or a linger expiring); None: when a state changes.
    wake_in: float | None = None

    def wake(self, seconds: float) -> None:
        if seconds > 0 and (self.wake_in is None or seconds < self.wake_in):
            self.wake_in = seconds


# -- Rendering --------------------------------------------------------------------------------------------------------


def render(job: JobState, notice: str | None = None) -> dict[str, Any]:
    """The content state (v2) of a card, as ``aps.content-state`` carries it, minus ``stale_ts``."""
    content: dict[str, Any] = {"v": 2, "phase": job.phase}
    if job.headline:
        content["headline"] = job.headline
    if job.detail:
        content["detail"] = job.detail
    if job.short:
        content["short"] = job.short
    if job.value is not None:
        content["value"] = dict(job.value)
    if job.progress is not None:
        content["progress"] = round(job.progress, 3)
    controls = []
    for index, control in enumerate(job.controls):
        item = {"id": control.id, "title": control.title}
        if index > 0:
            item["role"] = "secondary"
        controls.append(item)
    content["controls"] = controls
    if job.problem is not None:
        problem = job.problem
        data: dict[str, Any] = {"severity": problem.severity, "code": problem.code, "icon": problem.icon,
                                "problem": problem.problem, "message": problem.message, "chips": list(problem.chips)}
        if problem.task_id is not None:
            data["task_id"] = problem.task_id
        content["problem"] = data
    if job.url:
        content["url"] = job.url
    if notice or job.notice:
        content["notice"] = notice or job.notice
    return content


def card_message(job: JobState) -> str:
    """The text a start's quiet alert (and an alerting update) carries; the card itself shows the content state."""
    if job.problem is not None:
        return f"{job.problem.problem}. {job.problem.message}"
    return job.headline or job.title


def service_data(action: Push | End) -> dict[str, Any]:
    """The ``notify.mobile_app_<device>`` call for a planned push: core picks start, update or end from its tokens."""
    if isinstance(action, End):
        return {"message": "clear_notification", "data": {"tag": action.tag}}
    job = action.job
    data: dict[str, Any] = {
        "tag": action.tag,
        "live_update": True,
        "fenton_activity": {**action.content, "stale_ts": action.stale_ts},
        "fenton_attributes": {"kind": job.kind, "symbol": job.symbol, "tint": job.tint},
        "relevance_score": action.relevance,
    }
    if action.alert:
        data["live_alert"] = True
    if action.urgent:
        data["urgent"] = True
    return {"message": card_message(job), "title": job.title, "data": data}


def stale_for(content: dict[str, Any], now: float) -> int:
    """The stale date: ten minutes after a countdown ends, else half an hour from now (refreshed by the heartbeat)."""
    value = content.get("value") or {}
    if value.get("kind") == "countdown" and value.get("end_ts", 0) > now:
        return int(value["end_ts"]) + STALE_GRACE
    return int(now) + OPEN_ENDED_STALE_SECONDS


def significant(prev: dict[str, Any], new: dict[str, Any], job: JobState, card: Card, now: float) -> bool:
    """Whether the change from what the device shows is worth a push (spec section 4 "Updates on")."""
    tolerance = job.tolerance
    for key in (set(prev) | set(new)) - {"stale_ts", "notice"}:
        old, current = prev.get(key), new.get(key)
        if old == current:
            continue
        if key == "value" and isinstance(old, dict) and isinstance(current, dict) \
                and old.get("kind") == current.get("kind") == "countdown":
            if abs(old.get("end_ts", 0) - current.get("end_ts", 0)) > tolerance.end_seconds:
                return True
            continue
        if key == "progress" and isinstance(old, (int, float)) and isinstance(current, (int, float)):
            if abs(current - old) >= tolerance.progress_step and now - card.last_progress_push >= tolerance.progress_interval:
                return True
            continue
        return True
    return False


# -- Audience ---------------------------------------------------------------------------------------------------------


def audience_key(job: JobState) -> tuple[str, ...]:
    return tuple(sorted(job.audience)) if job.audience else ()


def pick(cards: list[JobState], person: str | None) -> dict[str, JobState]:
    """The card each tag shows to ``person``: the one whose audience includes them (None means everyone)."""
    chosen: dict[str, JobState] = {}
    for job in cards:
        if job.audience is not None and (person is None or person not in job.audience):
            continue
        chosen.setdefault(job.tag, job)
    return chosen


# -- The engine -------------------------------------------------------------------------------------------------------


class Engine:
    """Derives cards from states and plans the pushes each registration needs."""

    def __init__(self) -> None:
        self.memory = Memory()
        self.records: dict[str, ProblemRecord] = {}
        self.cards: dict[tuple[str, str], Card] = {}
        self.coalescer = Coalescer()
        self.first_seen: dict[str, float] = {}
        self.urgent_tags: set[str] = set()
        #: tag -> (user_id, text, created): a one-shot notice for the person whose button press failed.
        self.notices: dict[str, tuple[str, str, float]] = {}
        self.dismissed: dict[tuple[str, str], bool] = {}
        self.ended_at: dict[tuple[str, str], float] = {}
        self._prev_running: dict[tuple[str, tuple[str, ...]], JobState] = {}
        self._linger_until: dict[tuple[str, tuple[str, ...]], float] = {}
        self._linger_cards: dict[tuple[str, tuple[str, ...]], JobState] = {}
        self._lingered_out: set[tuple[str, tuple[str, ...]]] = set()
        self._unmapped_logged: set[str] = set()
        #: Current cards, before audience and slots: the result of the last ``refresh``.
        self.current: list[JobState] = []
        self.ended_records: list[ProblemRecord] = []
        #: Device -> code of a problem whose task was completed while the sensors still report it: it stays quiet until
        #: the sensors clear (Done on a robot that is still stuck must not bring the card straight back).
        self.suppressed: dict[str, str] = {}
        #: Until this time (Unix seconds) a card with no job is kept rather than ended: Home Assistant has only just
        #: started and the entities of other integrations may not be there yet.
        self.hold_until = 0.0

    # -- problems ----------------------------------------------------------------------------------------------------

    def _log_unmapped(self, code: str, raw: str | None) -> None:
        if kind_of(code) != "unmapped":
            return
        text_ = raw or code
        if text_ not in self._unmapped_logged:
            self._unmapped_logged.add(text_)
            _LOGGER.warning("fenton_home.problem_unmapped: %r (code %s)", text_, code)

    def raise_problem(self, job_id: str, code: str, raw: str | None, now: float, **fields: Any) -> ProblemRecord:
        """Open or update the service-raised problem on a device (``fenton_home.problem``)."""
        self.suppressed.pop(job_id, None)
        record = self.records.get(job_id)
        if record is None or record.code != code:
            record = ProblemRecord(job_id=job_id, code=code, raw=raw, source="service", created=now)
            self.records[job_id] = record
        elif raw:
            record.raw = raw
        for key, value in fields.items():
            if value is not None:
                setattr(record, key, value)
        self._log_unmapped(code, raw)
        return record

    def _sync_problems(self, found: list, now: float, running: frozenset[str] = frozenset()) -> None:
        """Open, update and end problem records.

        ``running`` holds the devices that are running their job right now. A problem with a linked task lives until
        the task is completed, or until its device's issue is clear and the device runs its job again (the card then
        shows the job and ends with it; the task stays open). Issue-clear alone never ends it: a robot's error clears
        seconds after it docks.
        """
        by_job = {signal.job_id: signal for signal in found}
        for job_id in list(self.suppressed):
            if job_id not in by_job:
                del self.suppressed[job_id]
        for job_id, signal in by_job.items():
            if self.suppressed.get(job_id) == signal.code:
                continue
            record = self.records.get(job_id)
            if record is None or (record.source == "sensor" and record.code != signal.code):
                record = ProblemRecord(job_id=job_id, code=signal.code, raw=signal.raw, source="sensor", created=now,
                                       room=signal.room, since=signal.since, sensor_seen=True)
                if old := self.records.get(job_id):
                    self.ended_records.append(old)
                self.records[job_id] = record
                self._log_unmapped(signal.code, signal.raw)
            else:
                record.sensor_seen = True
                record.room = signal.room or record.room
                record.since = record.since or signal.since
        for job_id, record in list(self.records.items()):
            if record.task_id is not None:
                gone = record.task_completed or (job_id in running and job_id not in by_job)
            elif record.awaiting_task(now):
                gone = False
            else:
                gone = job_id not in by_job and (record.source == "sensor" or record.sensor_seen
                                                  or now - record.created > PROBLEM_GRACE_SECONDS)
            if gone:
                self.ended_records.append(self.records.pop(job_id))
                if record.task_completed and job_id in by_job:
                    self.suppressed[job_id] = record.code

    def take_ended_records(self) -> list[ProblemRecord]:
        ended, self.ended_records = self.ended_records, []
        return ended

    def _problem_card(self, record: ProblemRecord, base: JobState | None, states: States, now: float,
                      tz: tzinfo | None, donetick_user: int | None = None) -> JobState:
        robot = ROBOTS.get(record.job_id)
        session = bool(robot and robot.coordinator and states.is_on(coordinator_entities(robot)["auto_cleaning"]))
        info: ProblemInfo = build_problem(
            record.code, record.raw, room=record.room, since=record.since or record.created, tz=tz,
            make_button=lambda button: ctl.problem_button(button, robot, session),
            task_id=record.task_id, make_done=lambda task_id: ctl.done(task_id, donetick_user),
            title=record.title, message=record.message)
        style = style_for(record.job_id)
        value = text("Needs help" if info.critical else "Attention")
        score = CRITICAL_SCORE if info.critical else WARNING_SCORE
        if base is None:
            return JobState(job_id=record.job_id, title=style.title, kind="problem", symbol=style.symbol,
                            tint=style.tint, phase="problem", value=value, controls=info.controls, url=style.url,
                            relevance=score, problem=info)
        return base.with_(phase="problem", value=value, headline=None, detail=None, progress=None, controls=info.controls,
                          relevance=score, problem=info, complete=False, complete_on_drop=False, linger_holds=False,
                          open_ended=True)

    # -- refresh -----------------------------------------------------------------------------------------------------

    def refresh(self, states: States, now: float, ctx: Ctx) -> list[JobState]:
        """Recompute the cards that should exist from a snapshot. Returns them (also kept in ``current``)."""
        ctx = Ctx(tz=ctx.tz, people=ctx.people, memory=self.memory, enabled=ctx.enabled)
        note_hot_flash(self.memory, states)
        jobs = derive(states, now, ctx)
        self._sync_problems(signals(states, now, ctx), now,
                            frozenset(job.job_id for job in jobs if not job.complete))

        running = {(job.tag, audience_key(job)): job for job in jobs if not job.complete}
        cards: list[JobState] = [job for job in jobs if not job.complete]
        produced_complete = {(job.tag, audience_key(job)): job for job in jobs if job.complete}
        # A running job that vanished with no stop finished on its own (laundry).
        for key, previous in self._prev_running.items():
            if key not in running and previous.complete_on_drop and key not in self._lingered_out:
                produced_complete.setdefault(key, as_complete(previous))
        self._prev_running = running
        # A job that runs again starts a fresh cycle: its previous Complete (if any) is history.
        for key in running:
            self._linger_until.pop(key, None)
            self._linger_cards.pop(key, None)
            self._lingered_out.discard(key)

        for key, job in produced_complete.items():
            if key in self._lingered_out:
                continue
            until = self._linger_until.setdefault(key, now + LINGER_SECONDS)
            if now >= until:
                self._lingered_out.add(key)
                self._linger_cards.pop(key, None)
                continue
            self._linger_cards[key] = job
            if key not in running:
                cards.append(job)
        # Complete cards whose source went away but that keep lingering (hot flash, laundry).
        for key, job in list(self._linger_cards.items()):
            until = self._linger_until.get(key, 0)
            if key in produced_complete or key in running:
                continue
            if job.linger_holds and now < until:
                cards.append(job)
            else:
                self._linger_cards.pop(key, None)
        # Forget lingers that are over and whose source no longer reports a finished job.
        for key in list(self._linger_until):
            if key not in produced_complete and key not in self._linger_cards and key not in running:
                self._linger_until.pop(key, None)
                self._lingered_out.discard(key)

        cards = self._overlay_problems(cards, states, now, ctx.tz)
        self.current = cards
        return cards

    def _overlay_problems(self, cards: list[JobState], states: States, now: float, tz: tzinfo | None) -> list[JobState]:
        out: list[JobState] = []
        used: set[str] = set()
        for job in cards:
            record = self.records.get(job.job_id)
            if record is not None and job.job_id not in used and job.audience is None:
                used.add(job.job_id)
                out.append(self._problem_card(record, job, states, now, tz))
            elif record is not None and job.job_id in used and job.audience is None:
                continue
            else:
                out.append(job)
        for job_id, record in self.records.items():
            if job_id not in used:
                out.append(self._problem_card(record, None, states, now, tz))
        return out

    # -- plan --------------------------------------------------------------------------------------------------------

    def plan(self, registrations: list[Registration], now: float) -> Plan:
        """The pushes each registration needs to show ``current``."""
        plan = Plan()
        tags = {job.tag for job in self.current}
        for tag in tags:
            self.first_seen.setdefault(tag, now)
        for tag in list(self.first_seen):
            if tag not in tags:
                del self.first_seen[tag]
        for key in list(self.dismissed):
            wanted = any(job.tag == key[1] for job in self.current)
            if not wanted:
                del self.dismissed[key]
        for registration in registrations:
            self._plan_registration(registration, now, plan)
        # Cards on registrations that are no longer eligible go away.
        eligible = {r.webhook_id for r in registrations}
        for (webhook_id, tag) in list(self.cards):
            if webhook_id not in eligible:
                plan.actions.append(End(webhook_id, tag, "not eligible"))
                self._ended((webhook_id, tag), now)
        self.urgent_tags -= {action.tag for action in plan.actions if isinstance(action, Push)}
        sent_to = {(a.tag, r.user_id) for a in plan.actions if isinstance(a, Push)
                   for r in registrations if r.webhook_id == a.webhook_id}
        for tag, (user_id, _, created) in list(self.notices.items()):
            if (tag, user_id) in sent_to or now - created > NOTICE_SECONDS:
                del self.notices[tag]
        return plan

    def _plan_registration(self, registration: Registration, now: float, plan: Plan) -> None:
        wh = registration.webhook_id
        wanted = pick(self.current, registration.person)
        candidates = []
        for tag, job in wanted.items():
            key = (wh, tag)
            if key in self.dismissed:
                if job.problem is not None and job.problem.critical and not self.dismissed[key]:
                    del self.dismissed[key]
                else:
                    continue
            candidates.append(Candidate(tag, job.relevance, job.problem is not None, self.first_seen.get(tag, now)))
        visible = [tag for (hook, tag) in self.cards if hook == wh]
        allocation = allocate(candidates, visible)
        show = set(allocation.show)

        for tag in visible:
            if tag not in show:
                reason = "bumped" if tag in allocation.end else "gone"
                if reason == "gone" and now < self.hold_until and tag not in wanted:
                    plan.wake(self.hold_until - now)
                    continue
                plan.actions.append(End(wh, tag, reason))
                self._ended((wh, tag), now)
        for tag in allocation.show:
            job = wanted[tag]
            card = self.cards.get((wh, tag))
            notice = self.notices.get(tag)
            notice_text = notice[1] if notice and notice[0] == registration.user_id else None
            if notice_text and self.coalescer.wait((wh, tag), now) > 0:
                plan.wake(self.coalescer.wait((wh, tag), now))
            new_session = card is not None and None not in (card.session_key, job.session_key) \
                and card.session_key != job.session_key
            if card is not None and (new_session or now - card.started_at >= MAX_AGE_SECONDS):
                plan.actions.append(End(wh, tag, "restart"))
                self._ended((wh, tag), now)
                card = None
            content = render(job, notice_text)
            if card is None:
                plan.actions.append(self._push(wh, tag, job, content, now, first=True))
                if job.open_ended:
                    plan.wake(HEARTBEAT_SECONDS)
                continue
            changed = significant(card.content, content, job, card, now) or bool(notice_text)
            heartbeat = job.open_ended and now - card.last_push >= HEARTBEAT_SECONDS
            if not (changed or heartbeat):
                if job.open_ended:
                    plan.wake(card.last_push + HEARTBEAT_SECONDS - now)
                continue
            wait = self.coalescer.wait((wh, tag), now)
            if wait > 0:
                plan.wake(wait)
                continue
            plan.actions.append(self._push(wh, tag, job, content, now, first=False))
            if job.open_ended:
                plan.wake(HEARTBEAT_SECONDS)
        # Lingers end on their own schedule, and a countdown that runs out re-evaluates its card.
        for key, until in self._linger_until.items():
            if key not in self._lingered_out:
                plan.wake(until - now)
        for job in self.current:
            if job.value and job.value.get("kind") == "countdown":
                plan.wake(job.value["end_ts"] - now + 1)

    def _push(self, webhook_id: str, tag: str, job: JobState, content: dict[str, Any], now: float, *,
              first: bool) -> Push:
        key = (webhook_id, tag)
        card = self.cards.get(key)
        critical = job.problem is not None and job.problem.critical
        alert = critical and not (card is not None and card.critical_sent)
        urgent = tag in self.urgent_tags
        stale = stale_for(content, now)
        previous_progress = card.last_progress if card else None
        progress = content.get("progress")
        if card is None:
            card = Card(started_at=now, last_push=now, content=content, session_key=job.session_key)
            self.cards[key] = card
        base = {k: v for k, v in content.items() if k != "notice"}
        card.content = base
        card.last_push = now
        card.session_key = job.session_key or card.session_key
        card.critical_sent = critical
        card.relevance = job.relevance
        if progress != previous_progress:
            card.last_progress, card.last_progress_push = progress, now
        self.coalescer.sent(key, now)
        return Push(webhook_id, tag, job, content, job.relevance, stale, urgent=urgent, alert=alert, first=first)

    def _ended(self, key: tuple[str, str], now: float) -> None:
        self.cards.pop(key, None)
        self.ended_at[key] = now
        self.coalescer.forget(key)

    # -- feedback from the world -------------------------------------------------------------------------------------

    def forget_webhook(self, webhook_id: str) -> int:
        """Drop every card and timer kept for a removed registration. Returns how many cards went."""
        gone = [key for key in self.cards if key[0] == webhook_id]
        for key in gone:
            del self.cards[key]
        for table in (self.dismissed, self.ended_at):
            for key in [k for k in table if k[0] == webhook_id]:
                del table[key]
        self.coalescer.forget_where(lambda k: isinstance(k, tuple) and k and k[0] == webhook_id)
        return len(gone)

    def push_failed(self, webhook_id: str, tag: str, now: float) -> None:
        """A push could not be sent: forget what the device shows so the next pass sends it again."""
        card = self.cards.get((webhook_id, tag))
        if card is not None:
            card.content = {}
            card.last_push = now - HEARTBEAT_SECONDS
            self.coalescer.forget((webhook_id, tag))

    def dismiss(self, webhook_id: str, tag: str, now: float) -> bool:
        """The person swiped a card away. Returns whether the engine took note (not the echo of its own end)."""
        key = (webhook_id, tag)
        card = self.cards.get(key)
        if card is None:
            return False
        if now - self.ended_at.get(key, -1e9) < DISMISS_ECHO_SECONDS:
            return False
        self.dismissed[key] = bool(card.critical_sent)
        self._ended(key, now)
        self.ended_at.pop(key, None)
        return True

    def mark_urgent(self, tag: str) -> None:
        """The next push for ``tag`` is a control confirmation: priority 10."""
        self.urgent_tags.add(tag)

    def set_notice(self, tag: str, user_id: str, notice: str, now: float = 0.0) -> None:
        self.notices[tag] = (user_id, notice, now)

    def force_end_tag(self, tag: str) -> None:
        """A confirmed Stop or Dock: forget the job so the card cannot linger or come back before the state settles."""
        for key in [k for k in self._prev_running if k[0] == tag]:
            self._prev_running.pop(key, None)
        for key in [k for k in self._linger_cards if k[0] == tag]:
            self._linger_cards.pop(key, None)

    def describe(self) -> dict[str, Any]:
        """Per-tag state for ``sensor.fenton_live_jobs``."""
        tags: dict[str, Any] = {}
        for job in self.current:
            entry = tags.setdefault(job.tag, {"phase": job.phase, "title": job.title, "relevance": job.relevance,
                                              "visible_on": [], "people": []})
            if job.audience:
                entry["people"].extend(sorted(job.audience))
            entry["problem"] = job.problem.code if job.problem else None
        for (webhook_id, tag) in self.cards:
            tags.setdefault(tag, {"visible_on": []})["visible_on"].append(webhook_id)
        return tags

