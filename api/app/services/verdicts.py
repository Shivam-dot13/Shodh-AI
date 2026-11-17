"""Turning raw per-test outcomes into a verdict and a score.

The worker reports *observations* (did the program finish, how long, how much output, did the
bytes match). The verdict rules live here, on the API side, so the judge cannot invent a
verdict and so the mapping is unit-testable without any execution at all.

A judge infrastructure failure is not in this table on purpose: it is a different state
(INFRA_FAILED), carries no verdict, and never affects a score.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import TestStatus, Verdict

# First failing test, by ordinal, names the verdict.
_FAILURE_VERDICT: dict[TestStatus, Verdict] = {
    TestStatus.WRONG_OUTPUT: Verdict.WRONG_ANSWER,
    TestStatus.RUNTIME_ERROR: Verdict.RUNTIME_ERROR,
    TestStatus.TIMEOUT: Verdict.TIME_LIMIT_EXCEEDED,
    TestStatus.MEMORY_EXCEEDED: Verdict.MEMORY_LIMIT_EXCEEDED,
    TestStatus.OUTPUT_EXCEEDED: Verdict.OUTPUT_LIMIT_EXCEEDED,
}


@dataclass(frozen=True)
class TestOutcome:
    ordinal: int
    status: TestStatus
    weight: int = 1


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    score: int
    tests_total: int
    tests_passed: int


def decide(
    *,
    compiled: bool,
    outcomes: list[TestOutcome],
    max_points: int,
) -> VerdictResult:
    if not compiled:
        return VerdictResult(
            verdict=Verdict.COMPILE_ERROR, score=0, tests_total=len(outcomes), tests_passed=0
        )
    if not outcomes:
        # An empty suite cannot be accepted; treat as wrong answer rather than a free pass.
        return VerdictResult(verdict=Verdict.WRONG_ANSWER, score=0, tests_total=0, tests_passed=0)

    ordered = sorted(outcomes, key=lambda o: o.ordinal)
    passed = [o for o in ordered if o.status is TestStatus.PASSED]
    total_weight = sum(max(0, o.weight) for o in ordered)
    passed_weight = sum(max(0, o.weight) for o in passed)

    if len(passed) == len(ordered):
        return VerdictResult(
            verdict=Verdict.ACCEPTED,
            score=max_points,
            tests_total=len(ordered),
            tests_passed=len(passed),
        )

    first_failure = next(o for o in ordered if o.status is not TestStatus.PASSED)
    verdict = _FAILURE_VERDICT.get(first_failure.status, Verdict.RUNTIME_ERROR)
    # Partial credit is floored, and a non-accepted attempt can never reach max_points.
    score = 0 if total_weight == 0 else min(max_points - 1, (max_points * passed_weight) // total_weight)
    return VerdictResult(
        verdict=verdict,
        score=max(0, score),
        tests_total=len(ordered),
        tests_passed=len(passed),
    )
