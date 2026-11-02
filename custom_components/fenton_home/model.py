"""The engine's data types: a job's card, its controls and the services behind them. No Home Assistant imports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .entities import tag_for


@dataclass(frozen=True)
class ServiceCall:
    """A Home Assistant action a control runs. ``data`` carries ``entity_id`` and fields like any action call."""

    domain: str
    service: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.domain}.{self.service}"


@dataclass(frozen=True)
class Expect:
    """The state a control waits for: ``entity_id`` reads one of ``states`` (or, ``invert``, none of them)."""

    entity_id: str
    states: frozenset[str]
    invert: bool = False

    def met(self, state: str | None) -> bool:
        hit = (state or "") in self.states
        return not hit if self.invert else hit


@dataclass(frozen=True)
class Control:
    """A button on a card. The phone sees ``id``, ``title`` and ``role``; the rest stays in Home Assistant.

    ``outcome`` is what a confirmed press does: ``end`` removes the card (Stop, Dock, Done), ``update`` pushes the new
    state (Pause, Resume).
    """

    id: str
    title: str
    calls: tuple[ServiceCall, ...] = ()
    expect: tuple[Expect, ...] = ()
    outcome: str = "end"
    #: ``donetick.complete_task`` and the like are confirmed by the todo list rather than an entity state.
    confirm: str = "states"  # states | task
    verb: str = ""  # "stop", for "Couldn't stop. Try again."


@dataclass(frozen=True)
class Tolerance:
    """How far a card may drift before it is worth a push."""

    end_seconds: float = 30.0
    progress_step: float = 0.05
    progress_interval: float = 300.0


@dataclass(frozen=True)
class ProblemInfo:
    """A problem as the card shows it (spec section 5). ``controls`` already include Done when there is a task."""

    severity: str  # critical | warning
    code: str
    icon: str
    problem: str
    message: str
    chips: tuple[str, ...] = ()
    task_id: int | None = None
    controls: tuple[Control, ...] = ()
    #: Fallback push tag and Donetick task title travel with the record, not the card.
    since: float | None = None

    @property
    def critical(self) -> bool:
        return self.severity == "critical"


@dataclass(frozen=True)
class JobState:
    """One card for one job (or one person's view of it), before it is rendered to a content state."""

    job_id: str
    title: str
    kind: str
    symbol: str
    tint: str
    phase: str  # running | paused | waiting | done | problem
    value: Mapping[str, Any] | None
    headline: str | None = None
    #: A short line under the title (hot flash: whose side, and who started it).
    detail: str | None = None
    #: A word or two for the compact island when the card has no time (vacuums), at most 10 characters.
    short: str | None = None
    progress: float | None = None
    controls: tuple[Control, ...] = ()
    url: str | None = None
    relevance: float = 55.0
    #: The job finished on its own: this is the "Complete" card, shown for the linger time.
    complete: bool = False
    #: When this running card disappears with no stop, it finished: show Complete (laundry).
    complete_on_drop: bool = False
    #: When this Complete card's source goes away the card still lingers to its deadline (laundry, hot flash).
    linger_holds: bool = False
    #: A change here ends the card and starts a new one (a vacuum's room or segment).
    session_key: str | None = None
    #: Person entity ids who see this card; None means everyone with S+H.
    audience: frozenset[str] | None = None
    problem: ProblemInfo | None = None
    tolerance: Tolerance = field(default_factory=Tolerance)
    #: A longer-lived card without an end time needs a stale-date refresh (spec stale rule).
    open_ended: bool = True
    #: A short notice shown in place of the value for one update ("Couldn't stop. Try again.").
    notice: str | None = None

    @property
    def tag(self) -> str:
        return tag_for(self.job_id)

    @property
    def is_problem(self) -> bool:
        return self.problem is not None

    def with_(self, **changes: Any) -> JobState:
        return replace(self, **changes)

    def control(self, control_id: str) -> Control | None:
        return next((c for c in self.controls if c.id == control_id), None)


def countdown(end_ts: float, start_ts: float | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"kind": "countdown", "end_ts": int(end_ts)}
    if start_ts is not None and start_ts < end_ts:
        value["start_ts"] = int(start_ts)
    return value


def countup(start_ts: float, paused_elapsed_s: float | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"kind": "countup", "start_ts": int(start_ts)}
    if paused_elapsed_s is not None:
        value["paused_elapsed_s"] = max(0, int(paused_elapsed_s))
    return value


def text(label: str) -> dict[str, Any]:
    return {"kind": "text", "text": label[:16]}


def percent() -> dict[str, Any]:
    return {"kind": "percent"}


@dataclass(frozen=True)
class PersonRef:
    """A ``person`` entity as the engine sees it."""

    entity_id: str
    user_id: str
    name: str

    @property
    def first(self) -> str:
        return (self.name.split() or [self.entity_id.split(".", 1)[-1]])[0]
