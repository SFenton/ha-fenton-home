"""Slots, queueing, bumping and coalescing (spec section 6)."""

from __future__ import annotations

from fenton_home.slots import Candidate, Coalescer, allocate


def job(tag: str, relevance: float = 55, since: float = 0) -> Candidate:
    return Candidate(tag, relevance, False, since)


def problem(tag: str, relevance: float = 100, since: float = 0) -> Candidate:
    return Candidate(tag, relevance, True, since)


def test_four_jobs_fit_and_a_fifth_waits_in_arrival_order() -> None:
    cands = [job(f"j{i}", since=i) for i in range(6)]
    result = allocate(cands, [])
    assert result.show == ["j0", "j1", "j2", "j3"] and result.start == result.show
    assert result.queued == ["j4", "j5"] and result.end == []


def test_a_slot_that_frees_goes_to_the_longest_waiting_job() -> None:
    cands = [job("j1", since=1), job("j2", since=2), job("j4", since=4), job("j5", since=5), job("j6", since=6)]
    # j3 just finished; j5 was admitted earlier than j4 queued behind it.
    result = allocate(cands, ["j1", "j2", "j5"])
    assert result.start == ["j4"] and result.queued == ["j6"] and len(result.show) == 4
    more = allocate(cands + [job("j7", since=7)], ["j1", "j2", "j5"])
    assert more.start == ["j4"] and more.queued == ["j6", "j7"]


def test_cards_that_are_showing_keep_their_slots_whatever_their_relevance() -> None:
    cands = [job("old", 10, since=0), job("a", 90, since=1), job("b", 80, since=2), job("c", 70, since=3),
             job("new", 99, since=4)]
    result = allocate(cands, ["old", "a", "b", "c"])
    assert result.end == [] and result.queued == ["new"]


def test_a_problem_takes_the_free_fifth_slot() -> None:
    cands = [job(f"j{i}", since=i) for i in range(4)] + [problem("p")]
    result = allocate(cands, ["j0", "j1", "j2", "j3"])
    assert result.start == ["p"] and result.end == [] and len(result.show) == 5


def test_a_problem_bumps_the_lowest_relevance_job_when_all_five_are_used() -> None:
    cands = [job("a", 70), job("b", 60), job("c", 50, since=1), job("d", 50, since=2), problem("p1", 90, since=3),
             problem("p2", 100, since=4)]
    result = allocate(cands, ["a", "b", "c", "d", "p1"])
    # Among equal relevance, the one that started latest goes first.
    assert result.end == ["d"] and result.start == ["p2"]
    assert "d" in result.queued and "d" not in result.show
    assert set(result.show) == {"a", "b", "c", "p1", "p2"}


def test_problems_are_admitted_most_severe_first() -> None:
    cands = [problem("warn", 90, since=0), problem("crit", 100, since=1)] + [job(f"j{i}", since=i) for i in range(4)]
    result = allocate(cands, ["j0", "j1", "j2", "j3"])
    # The critical one takes the free slot; the warning bumps the job that matters least (equal relevance: the newest).
    assert result.start == ["crit", "warn"] and result.end == ["j3"]
    # With all five slots held by problems, a further problem waits.
    five = [problem(f"p{i}", 100 - i) for i in range(6)]
    result = allocate(five, [f"p{i}" for i in range(5)])
    assert result.queued == ["p5"] and result.end == []


def test_nothing_visible_is_ended_by_allocate_for_a_job_that_is_gone() -> None:
    result = allocate([job("a")], ["a", "vanished"])
    assert result.show == ["a"] and result.end == []


def test_a_job_that_becomes_a_problem_frees_a_job_slot() -> None:
    cands = [problem("a")] + [job(f"j{i}", since=i + 1) for i in range(4)]
    result = allocate(cands, ["a", "j0", "j1", "j2"])
    assert result.start == ["j3"] and len(result.show) == 5


def test_coalescer_allows_one_push_per_window() -> None:
    coalescer = Coalescer(2.0)
    key = ("hook", "job.fan.guest")
    assert coalescer.wait(key, 100) == 0
    coalescer.sent(key, 100)
    assert coalescer.wait(key, 100.5) == 1.5 and coalescer.wait(key, 102.0) == 0
    assert coalescer.wait(("hook", "other"), 100.5) == 0
    assert coalescer.last(key) == 100
    coalescer.forget(key)
    assert coalescer.wait(key, 100.5) == 0 and coalescer.last(key) is None
