"""Slots, queueing and coalescing (spec section 6). Pure, so the rules are unit-tested alone.

A device can show about five activities (``targetMaximumExceeded`` is the observed limit; Apple publishes no number).
The engine keeps four for jobs and one for a problem:

- a fifth job waits in the queue and starts when a slot frees;
- a problem that finds all five slots used ends the lowest-relevance job and takes its slot (that job goes back in the
  queue and starts again when a slot frees);
- cards that are already showing keep their slot, so the order never churns.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .entities import COALESCE_SECONDS, SLOTS_JOBS, SLOTS_TOTAL


@dataclass(frozen=True)
class Candidate:
    """A card that wants a slot."""

    tag: str
    relevance: float
    is_problem: bool
    #: When it first wanted a slot (Unix seconds): the queue is first come, first served.
    since: float = 0.0


@dataclass
class Allocation:
    show: list[str] = field(default_factory=list)  # visible after this pass, in slot order
    start: list[str] = field(default_factory=list)  # newly admitted
    end: list[str] = field(default_factory=list)  # visible before and bumped now
    queued: list[str] = field(default_factory=list)  # waiting for a slot


def allocate(candidates: Iterable[Candidate], visible: Iterable[str], *, total: int = SLOTS_TOTAL,
             jobs: int = SLOTS_JOBS) -> Allocation:
    """Decide which candidates show. ``visible`` are the tags this device is showing now."""
    wanted = {c.tag: c for c in candidates}
    shown = [wanted[tag] for tag in dict.fromkeys(visible) if tag in wanted]
    result = Allocation()

    def job_count() -> int:
        return sum(1 for c in shown if not c.is_problem)

    waiting_problems = sorted((c for c in wanted.values() if c.is_problem and c not in shown),
                              key=lambda c: (-c.relevance, c.since, c.tag))
    for problem in waiting_problems:
        if len(shown) < total:
            shown.append(problem)
            result.start.append(problem.tag)
            continue
        victims = sorted((c for c in shown if not c.is_problem), key=lambda c: (c.relevance, -c.since, c.tag))
        if victims:
            victim = victims[0]
            shown.remove(victim)
            result.end.append(victim.tag)
            shown.append(problem)
            result.start.append(problem.tag)
        else:
            result.queued.append(problem.tag)
    waiting_jobs = sorted((c for c in wanted.values() if not c.is_problem and c not in shown
                           and c.tag not in result.end), key=lambda c: (c.since, c.tag))
    for job in waiting_jobs:
        if len(shown) < total and job_count() < jobs:
            shown.append(job)
            result.start.append(job.tag)
        else:
            result.queued.append(job.tag)
    # A card bumped this pass waits for a slot like any other job.
    result.queued.extend(tag for tag in result.end if tag not in result.queued)
    result.show = [c.tag for c in shown]
    return result


class Coalescer:
    """At most one push per key (tag on a device) per ``window`` seconds: a burst collapses to the latest state."""

    def __init__(self, window: float = COALESCE_SECONDS) -> None:
        self.window = window
        self._last: dict[object, float] = {}

    def wait(self, key: object, now: float) -> float:
        """Seconds until a push for ``key`` is allowed (0 when it is allowed now)."""
        last = self._last.get(key)
        return 0.0 if last is None else max(0.0, last + self.window - now)

    def sent(self, key: object, now: float) -> None:
        self._last[key] = now

    def forget(self, key: object) -> None:
        self._last.pop(key, None)

    def last(self, key: object) -> float | None:
        return self._last.get(key)
