"""Applying a verdict to the standings.

The whole scoring write path is one function: recompute one (contest, user, problem) cell from
the authoritative judged attempts of that cell and store the result. Properties this buys:

* Idempotent. Replaying the same completion recomputes the same value, so a duplicated
  delivery cannot award points twice. There is no += anywhere.
* Order independent. Results that complete out of order land in the same final state, because
  the cell is a function of the stored set of attempts, not of arrival order.
* Rejudge safe. A late result from a replaced attempt is written to its own attempt row but is
  not authoritative (a higher attempt_no exists), so the cell does not move.

Concurrency is handled by taking the cell row FOR UPDATE: two workers completing two
submissions of the same cell serialize on that row, and the loser recomputes from a set that
already includes the winner.
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
    session: Session, contest: Contest, user_id: uuid.UUID, problem_id: uuid.UUID
) -> CellResult:
    """Recompute and persist one standings cell. Caller owns the transaction."""
    cell_row = _lock_cell(session, contest.id, user_id, problem_id)
    rows = judged_rows_for_cell(session, contest.id, user_id, problem_id)
    result = score_cell(
        mode=contest.scoring_mode,
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

    Used by the organizer-facing consistency check and after a bulk rejudge. Returns the number
    of cells written.
    """
    pairs = session.execute(
        text(
            """
            SELECT DISTINCT s.user_id, s.problem_id
            FROM submissions s
            WHERE s.contest_id = :contest_id
            """
        ),
        {"contest_id": contest.id},
    ).all()
    for user_id, problem_id in pairs:
        recompute_cell(session, contest, user_id, problem_id)
    return len(pairs)
