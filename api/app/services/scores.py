"""Applying a verdict to the standings.

The write path is one function: recompute a (contest, user, problem) cell from that cell's
judged attempts and store it. No += anywhere, which is what makes replays, out-of-order
completions and rejudges all the same case.

Two workers finishing submissions in the same cell serialize on the cell row (FOR UPDATE); the
loser recomputes from a set that already has the winner in it.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..domain.enums import AttemptState, Verdict
from ..models import Contest, ProblemScore
from .scoring import CellResult, JudgedSubmission, score_cell

log = logging.getLogger(__name__)


def judged_rows_for_cell(
    session: Session, contest_id: uuid.UUID, user_id: uuid.UUID, problem_id: uuid.UUID
) -> list[JudgedSubmission]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (s.id)
                   s.id AS submission_id, s.submitted_at,
                   a.id AS attempt_id, a.verdict, a.score, a.max_score
            FROM submissions s
            JOIN judging_attempts a ON a.submission_id = s.id AND a.state = :completed
            WHERE s.contest_id = :contest_id AND s.user_id = :user_id AND s.problem_id = :problem_id
            ORDER BY s.id, a.attempt_no DESC
            """
        ),
        {
            "contest_id": contest_id,
            "user_id": user_id,
            "problem_id": problem_id,
            "completed": AttemptState.COMPLETED.value,
        },
    ).mappings()
    return [
        JudgedSubmission(
            submission_id=row["submission_id"],
            attempt_id=row["attempt_id"],
            submitted_at=row["submitted_at"],
            verdict=Verdict(row["verdict"]),
            score=row["score"],
            max_score=row["max_score"],
        )
        for row in rows
    ]


def _lock_cell(
    session: Session, contest_id: uuid.UUID, user_id: uuid.UUID, problem_id: uuid.UUID
) -> ProblemScore:
    stmt = (
        pg_insert(ProblemScore)
        .values(id=uuid.uuid4(), contest_id=contest_id, user_id=user_id, problem_id=problem_id)
        .on_conflict_do_nothing(index_elements=["contest_id", "user_id", "problem_id"])
    )
    session.execute(stmt)
    return session.execute(
        select(ProblemScore)
        .where(
            ProblemScore.contest_id == contest_id,
            ProblemScore.user_id == user_id,
            ProblemScore.problem_id == problem_id,
        )
        .with_for_update()
    ).scalar_one()


def recompute_cell(
    session: Session, contest: Contest, user_id: uuid.UUID, problem_id: uuid.UUID, *, bump: bool = True
) -> CellResult:
    """Recompute and persist one standings cell. Caller owns the transaction.

    `bump=False` is for a bulk rebuild, which bumps the contest counter once at the end. Lock
    order matters here: a verdict transaction takes one cell row and then the contest row, so a
    rebuild must not interleave contest-row updates between cell locks or the two can deadlock.
    """
    cell_row = _lock_cell(session, contest.id, user_id, problem_id)
    rows = judged_rows_for_cell(session, contest.id, user_id, problem_id)
    result = score_cell(
        penalty_per_rejection=contest.penalty_minutes,
        contest_start=contest.starts_at,
        rows=rows,
    )
    cell_row.points = result.points
    cell_row.solved = result.solved
    cell_row.solved_at = result.solved_at
    cell_row.penalty_minutes = result.penalty_minutes
    cell_row.rejected_before_solve = result.rejected_before_solve
    cell_row.authoritative_attempt_id = result.authoritative_attempt_id
    cell_row.submission_id = result.submission_id
    cell_row.last_progress_at = result.last_progress_at
    session.flush()
    if bump:
        bump_standings_version(session, contest.id)
    log.info(
        "standings cell recomputed",
        extra={
            "contest_id": str(contest.id),
            "scored_user_id": str(user_id),
            "problem_id": str(problem_id),
            "points": result.points,
            "solved": result.solved,
            "judged_submissions": len(rows),
        },
    )
    return result


def bump_standings_version(session: Session, contest_id: uuid.UUID) -> int:
    """Monotonic marker clients can poll on. Bumped inside the verdict transaction."""
    return session.execute(
        update(Contest)
        .where(Contest.id == contest_id)
        .values(standings_version=Contest.standings_version + 1)
        .returning(Contest.standings_version)
    ).scalar_one()


def rebuild_all_cells(session: Session, contest: Contest) -> int:
    """Recompute every cell of a contest from authoritative records.

    Used by the organizer-facing repair path and after a bulk rejudge. Cells are locked in a
    stable order and the contest counter is bumped once at the end, so a rebuild running while
    verdicts are being stored cannot deadlock with them. Returns the number of cells written.
    """
    pairs = session.execute(
        text(
            """
            SELECT DISTINCT s.user_id, s.problem_id
            FROM submissions s
            WHERE s.contest_id = :contest_id
            ORDER BY s.user_id, s.problem_id
            """
        ),
        {"contest_id": contest.id},
    ).all()
    for user_id, problem_id in pairs:
        recompute_cell(session, contest, user_id, problem_id, bump=False)
    if pairs:
        bump_standings_version(session, contest.id)
    return len(pairs)
