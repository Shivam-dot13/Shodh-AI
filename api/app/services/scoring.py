"""Contest rules as pure functions.

Nothing here touches the database or the clock, so the rules are unit-testable and the same
code backs both the incremental serving path and the full rebuild. Scoring is defined as a
*function of the authoritative judged attempts*, never as an increment, which is why a
duplicated or out-of-order completion cannot change a score twice.

Authoritative attempt of a submission
    the COMPLETED attempt with the highest attempt_no. An attempt that failed for judge
    infrastructure reasons has no verdict and is therefore never authoritative, so a runner
    crash can never lower a score. A late result from an attempt that a rejudge has already
    replaced cannot become authoritative either, because a higher attempt_no exists.

ICPC mode
    points   = max_points of the pinned problem version on the first accepted submission
    penalty  = minutes from contest start to that submission
               + contest.penalty_minutes * (judged, non-accepted submissions before it)
    rank by    solved desc, penalty asc, last_solved_at asc, user_id asc

POINTS mode
    points   = best score over judged submissions (earliest submission wins a tie)
    penalty  = not used
    rank by    points desc, last_progress_at asc, user_id asc

Both orderings end in user_id, so the order is total and therefore stable across pages,
processes and rebuilds.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.enums import ScoringMode, Verdict


@dataclass(frozen=True)
class JudgedSubmission:
    """One submission of one (user, problem) whose authoritative attempt has completed."""

    submission_id: uuid.UUID
    attempt_id: uuid.UUID
    submitted_at: datetime
    verdict: Verdict
    score: int
    max_score: int

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.ACCEPTED


@dataclass(frozen=True)
class CellResult:
    points: int = 0
    solved: bool = False
    solved_at: datetime | None = None
    penalty_minutes: int = 0
    rejected_before_solve: int = 0
    authoritative_attempt_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None
    last_progress_at: datetime | None = None


def _minutes_since(start: datetime, when: datetime) -> int:
    return max(0, math.floor((when - start).total_seconds() / 60))


def score_cell(
    *,
    mode: ScoringMode,
    penalty_per_rejection: int,
    contest_start: datetime,
    rows: list[JudgedSubmission],
) -> CellResult:
    """Score one (user, problem) cell. `rows` need not be sorted."""
    ordered = sorted(rows, key=lambda r: (r.submitted_at, str(r.submission_id)))
    if not ordered:
        return CellResult()

    if mode is ScoringMode.ICPC:
        rejected = 0
        for row in ordered:
            if row.accepted:
                return CellResult(
                    points=row.max_score,
                    solved=True,
                    solved_at=row.submitted_at,
                    penalty_minutes=_minutes_since(contest_start, row.submitted_at)
                    + penalty_per_rejection * rejected,
                    rejected_before_solve=rejected,
                    authoritative_attempt_id=row.attempt_id,
                    submission_id=row.submission_id,
                    last_progress_at=row.submitted_at,
                )
            rejected += 1
        return CellResult(rejected_before_solve=rejected, last_progress_at=ordered[-1].submitted_at)

    # Highest score wins; among equal scores the earliest submission wins.
    best = max(enumerate(ordered), key=lambda pair: (pair[1].score, -pair[0]))[1]
    return CellResult(
        points=best.score,
        solved=best.accepted,
        solved_at=best.submitted_at if best.accepted else None,
        penalty_minutes=0,
        rejected_before_solve=sum(1 for r in ordered if not r.accepted),
        authoritative_attempt_id=best.attempt_id,
        submission_id=best.submission_id,
        last_progress_at=best.submitted_at,
    )


@dataclass
class UserTotals:
    user_id: uuid.UUID
    display_name: str = ""
    solved: int = 0
    points: int = 0
    penalty_minutes: int = 0
    last_solved_at: datetime | None = None
    last_progress_at: datetime | None = None
    cells: dict[uuid.UUID, CellResult] = field(default_factory=dict)


def aggregate_user(user_id: uuid.UUID, cells: dict[uuid.UUID, CellResult], display_name: str = "") -> UserTotals:
    totals = UserTotals(user_id=user_id, display_name=display_name, cells=dict(cells))
    for cell in cells.values():
        totals.points += cell.points
        if cell.solved:
            totals.solved += 1
            totals.penalty_minutes += cell.penalty_minutes
            if cell.solved_at and (totals.last_solved_at is None or cell.solved_at > totals.last_solved_at):
                totals.last_solved_at = cell.solved_at
        if cell.last_progress_at and (
            totals.last_progress_at is None or cell.last_progress_at > totals.last_progress_at
        ):
            totals.last_progress_at = cell.last_progress_at
    return totals


def _tiebreak_time(value: datetime | None) -> tuple[int, float]:
    # Rows with no time sort last, deterministically.
    return (1, 0.0) if value is None else (0, value.timestamp())


def rank_key(mode: ScoringMode, totals: UserTotals) -> tuple:
    if mode is ScoringMode.ICPC:
        return (
            -totals.solved,
            totals.penalty_minutes,
            _tiebreak_time(totals.last_solved_at),
            str(totals.user_id),
        )
    return (-totals.points, _tiebreak_time(totals.last_progress_at), str(totals.user_id))


@dataclass
class StandingRow:
    rank: int
    user_id: uuid.UUID
    display_name: str
    solved: int
    points: int
    penalty_minutes: int
    last_solved_at: datetime | None
    cells: dict[uuid.UUID, CellResult]


def rank_users(mode: ScoringMode, users: list[UserTotals]) -> list[StandingRow]:
    """Dense ranking: equal scores share a rank, order is still total."""
    ordered = sorted(users, key=lambda t: rank_key(mode, t))
    rows: list[StandingRow] = []
    previous_score: tuple | None = None
    rank = 0
    for index, totals in enumerate(ordered, start=1):
        if mode is ScoringMode.ICPC:
            score_part = (totals.solved, totals.penalty_minutes)
        else:
            score_part = (totals.points,)
        if score_part != previous_score:
            rank = index
            previous_score = score_part
        rows.append(
            StandingRow(
                rank=rank,
                user_id=totals.user_id,
                display_name=totals.display_name,
                solved=totals.solved,
                points=totals.points,
                penalty_minutes=totals.penalty_minutes,
                last_solved_at=totals.last_solved_at,
                cells=totals.cells,
            )
        )
    return rows
