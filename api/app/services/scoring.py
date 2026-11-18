"""Contest rules as pure functions.

No clock, no database, so the rules are unit-testable on their own and the same code backs both
the live write path and a full rebuild. Scoring is a function of the authoritative judged
attempts, never an increment, which is why a duplicated or out-of-order completion cannot change
a score twice.

Authoritative attempt of a submission: the COMPLETED attempt with the highest attempt_no. An
attempt that failed for judge-infrastructure reasons has no verdict and so is never
authoritative, meaning a runner crash cannot lower a score. A late result from an attempt a
rejudge has replaced cannot become authoritative either, because a higher attempt_no exists.

Scoring (ICPC style):
    points  = max_points of the pinned problem version, on the first accepted submission
    penalty = minutes from contest start to that submission
              + penalty_minutes for each judged, non-accepted submission before it
    rank by   solved desc, penalty asc, last_solved_at asc, user_id asc

The order ends in user_id, so it is total: stable across pages, processes and rebuilds.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.enums import Verdict


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
    penalty_per_rejection: int,
    contest_start: datetime,
    rows: list[JudgedSubmission],
) -> CellResult:
    """Score one (user, problem) cell. `rows` need not be sorted."""
    ordered = sorted(rows, key=lambda r: (r.submitted_at, str(r.submission_id)))
    if not ordered:
        return CellResult()

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
    # Nothing accepted: no points and no penalty. Penalty is only ever paid for a problem that
    # was eventually solved.
    return CellResult(rejected_before_solve=rejected, last_progress_at=ordered[-1].submitted_at)


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


def rank_key(totals: UserTotals) -> tuple:
    return (
        -totals.solved,
        totals.penalty_minutes,
        _tiebreak_time(totals.last_solved_at),
        str(totals.user_id),
    )


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


def rank_users(users: list[UserTotals]) -> list[StandingRow]:
    """Competition ranking: equal scores share a rank and the next rank skips (1, 1, 3)."""
    ordered = sorted(users, key=rank_key)
    rows: list[StandingRow] = []
    previous_score: tuple | None = None
    rank = 0
    for index, totals in enumerate(ordered, start=1):
        # Only the score-significant columns decide a tie; last_solved_at just fixes the order.
        score_part = (totals.solved, totals.penalty_minutes)
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
