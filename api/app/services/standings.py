"""Leaderboard: the serving path, a deliberately naive path, and a full rebuild.

serve     reads the maintained problem_scores cells. This is what clients get.
naive     recomputes the same answer from submissions + judging_attempts on every request.
          It is the pre-optimization serving path, kept so the Stage 4 bottleneck study can
          compare query plans and latency on the same data.
rebuild   recomputes every cell in Python with the same pure functions as the write path.

All three must produce identical ranking output. tests/integration/test_standings_consistency.py
is what holds that claim up.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.enums import AttemptState, Verdict
from ..models import Contest
from .scoring import (
    CellResult,
    JudgedSubmission,
    StandingRow,
    UserTotals,
    aggregate_user,
    rank_users,
    score_cell,
)

# Only the score columns decide a tie; the rest fixes the order so paging is stable.
_ORDER_RANK = "solved DESC, penalty_minutes ASC"
_ORDER_TOTAL = "solved DESC, penalty_minutes ASC, last_solved_at ASC NULLS LAST, user_id ASC"

_SERVE_AGG = """
SELECT cp.user_id AS user_id,
       u.display_name AS display_name,
       COALESCE(SUM(CASE WHEN ps.solved THEN 1 ELSE 0 END), 0) AS solved,
       COALESCE(SUM(ps.points), 0) AS points,
       COALESCE(SUM(CASE WHEN ps.solved THEN ps.penalty_minutes ELSE 0 END), 0) AS penalty_minutes,
       MAX(CASE WHEN ps.solved THEN ps.solved_at END) AS last_solved_at,
       MAX(ps.last_progress_at) AS last_progress_at
FROM contest_participants cp
JOIN users u ON u.id = cp.user_id
LEFT JOIN problem_scores ps ON ps.contest_id = cp.contest_id AND ps.user_id = cp.user_id
WHERE cp.contest_id = :contest_id
GROUP BY cp.user_id, u.display_name
"""

# Derives every cell on the fly: pick each submission's latest COMPLETED attempt, then fold the
# scoring rules into SQL. problem_scores is not involved.
_NAIVE_AGG = """
WITH judged AS (
    SELECT DISTINCT ON (s.id)
           s.id AS submission_id, s.user_id, s.problem_id, s.submitted_at,
           a.id AS attempt_id, a.verdict, a.max_score
    FROM submissions s
    JOIN judging_attempts a ON a.submission_id = s.id AND a.state = 'COMPLETED'
    WHERE s.contest_id = :contest_id
    ORDER BY s.id, a.attempt_no DESC
), ranked AS (
    SELECT j.*,
           ROW_NUMBER() OVER (
               PARTITION BY j.user_id, j.problem_id ORDER BY j.submitted_at, j.submission_id
           ) AS seq
    FROM judged j
), windowed AS (
    SELECT r.*,
           MIN(CASE WHEN r.verdict = 'ACCEPTED' THEN r.seq END)
               OVER (PARTITION BY r.user_id, r.problem_id) AS first_ac_seq,
           MIN(CASE WHEN r.verdict = 'ACCEPTED' THEN r.submitted_at END)
               OVER (PARTITION BY r.user_id, r.problem_id) AS ac_at,
           FIRST_VALUE(r.max_score) OVER (
               PARTITION BY r.user_id, r.problem_id
               ORDER BY (r.verdict = 'ACCEPTED') DESC, r.seq
           ) AS ac_points
    FROM ranked r
), cells AS (
    SELECT w.user_id,
           w.problem_id,
           BOOL_OR(w.verdict = 'ACCEPTED') AS solved,
           MIN(w.ac_at) AS solved_at,
           MAX(w.ac_points) AS ac_points,
           MAX(w.submitted_at) AS last_submitted_at,
           COUNT(*) FILTER (
               WHERE w.verdict <> 'ACCEPTED'
                 AND (w.first_ac_seq IS NULL OR w.seq < w.first_ac_seq)
           ) AS rejected_before
    FROM windowed w
    GROUP BY w.user_id, w.problem_id
), per_user AS (
    SELECT c.user_id,
           SUM(CASE WHEN c.solved THEN 1 ELSE 0 END) AS solved,
           SUM(CASE WHEN c.solved THEN c.ac_points ELSE 0 END) AS points,
           SUM(CASE WHEN c.solved
                    THEN CAST(GREATEST(0, FLOOR(
                             EXTRACT(EPOCH FROM (c.solved_at - CAST(:starts_at AS timestamptz))) / 60
                         )) AS int) + :penalty * c.rejected_before
                    ELSE 0 END) AS penalty_minutes,
           MAX(CASE WHEN c.solved THEN c.solved_at END) AS last_solved_at,
           MAX(COALESCE(c.solved_at, c.last_submitted_at)) AS last_progress_at
    FROM cells c
    GROUP BY c.user_id
)
SELECT cp.user_id AS user_id,
       u.display_name AS display_name,
       COALESCE(pu.solved, 0) AS solved,
       COALESCE(pu.points, 0) AS points,
       COALESCE(pu.penalty_minutes, 0) AS penalty_minutes,
       pu.last_solved_at AS last_solved_at,
       pu.last_progress_at AS last_progress_at
FROM contest_participants cp
JOIN users u ON u.id = cp.user_id
LEFT JOIN per_user pu ON pu.user_id = cp.user_id
WHERE cp.contest_id = :contest_id
"""


@dataclass
class StandingsPage:
    rows: list[StandingRow]
    total: int
    standings_version: int
    source: str
    generated_at: datetime


def _wrap_ranked(agg_sql: str) -> str:
    return f"""
SELECT ranked.*, COUNT(*) OVER () AS total_rows FROM (
  SELECT agg.*, RANK() OVER (ORDER BY {_ORDER_RANK}) AS rank
  FROM ({agg_sql}) agg
) ranked
ORDER BY {_ORDER_TOTAL}
LIMIT :limit OFFSET :offset
"""


def _naive_params(contest: Contest, limit: int, offset: int) -> dict:
    return {
        "contest_id": contest.id,
        "limit": limit,
        "offset": offset,
        "starts_at": contest.starts_at,
        "penalty": contest.penalty_minutes,
    }


def _rows_to_page(session: Session, contest: Contest, result, source: str) -> StandingsPage:
    records = result.mappings().all()
    user_ids = [r["user_id"] for r in records]
    cells = _fetch_cells(session, contest, user_ids)
    rows = [
        StandingRow(
            rank=int(r["rank"]),
            user_id=r["user_id"],
            display_name=r["display_name"],
            solved=int(r["solved"]),
            points=int(r["points"]),
            penalty_minutes=int(r["penalty_minutes"]),
            last_solved_at=r["last_solved_at"],
            cells=cells.get(r["user_id"], {}),
        )
        for r in records
    ]
    return StandingsPage(
        rows=rows,
        total=int(records[0]["total_rows"]) if records else 0,
        standings_version=contest.standings_version,
        source=source,
        generated_at=datetime.now().astimezone(),
    )


def _fetch_cells(
    session: Session, contest: Contest, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[uuid.UUID, CellResult]]:
    if not user_ids:
        return {}
    rows = session.execute(
        text(
            """
            SELECT user_id, problem_id, points, solved, solved_at, penalty_minutes,
                   rejected_before_solve, authoritative_attempt_id, submission_id, last_progress_at
            FROM problem_scores
            WHERE contest_id = :contest_id AND user_id = ANY(:user_ids)
            """
        ),
        {"contest_id": contest.id, "user_ids": user_ids},
    ).mappings()
    out: dict[uuid.UUID, dict[uuid.UUID, CellResult]] = defaultdict(dict)
    for row in rows:
        out[row["user_id"]][row["problem_id"]] = CellResult(
            points=row["points"],
            solved=row["solved"],
            solved_at=row["solved_at"],
            penalty_minutes=row["penalty_minutes"],
            rejected_before_solve=row["rejected_before_solve"],
            authoritative_attempt_id=row["authoritative_attempt_id"],
            submission_id=row["submission_id"],
            last_progress_at=row["last_progress_at"],
        )
    return out


def serve(session: Session, contest: Contest, *, limit: int = 50, offset: int = 0) -> StandingsPage:
    result = session.execute(
        text(_wrap_ranked(_SERVE_AGG)),
        {"contest_id": contest.id, "limit": limit, "offset": offset},
    )
    return _rows_to_page(session, contest, result, "standings")


def naive(session: Session, contest: Contest, *, limit: int = 50, offset: int = 0) -> StandingsPage:
    result = session.execute(text(_wrap_ranked(_NAIVE_AGG)), _naive_params(contest, limit, offset))
    return _rows_to_page(session, contest, result, "naive")


def explain(session: Session, contest: Contest, source: str, *, limit: int = 50) -> str:
    """EXPLAIN (ANALYZE, BUFFERS) for either serving path, for the performance report."""
    if source == "naive":
        sql, params = _NAIVE_AGG, _naive_params(contest, limit, 0)
    else:
        sql, params = _SERVE_AGG, {"contest_id": contest.id, "limit": limit, "offset": 0}
    rows = session.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, VERBOSE) " + _wrap_ranked(sql)), params
    ).scalars().all()
    return "\n".join(rows)


def authoritative_rows(
    session: Session, contest_id: uuid.UUID
) -> dict[tuple[uuid.UUID, uuid.UUID], list[JudgedSubmission]]:
    """Every judged submission of a contest, grouped by (user, problem).

    Reads only append-only records: submissions and their COMPLETED attempts.
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (s.id)
                   s.id AS submission_id, s.user_id, s.problem_id, s.submitted_at,
                   a.id AS attempt_id, a.verdict, a.score, a.max_score
            FROM submissions s
            JOIN judging_attempts a ON a.submission_id = s.id AND a.state = :completed
            WHERE s.contest_id = :contest_id
            ORDER BY s.id, a.attempt_no DESC
            """
        ),
        {"contest_id": contest_id, "completed": AttemptState.COMPLETED.value},
    ).mappings()
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list[JudgedSubmission]] = defaultdict(list)
    for row in rows:
        grouped[(row["user_id"], row["problem_id"])].append(
            JudgedSubmission(
                submission_id=row["submission_id"],
                attempt_id=row["attempt_id"],
                submitted_at=row["submitted_at"],
                verdict=Verdict(row["verdict"]),
                score=row["score"],
                max_score=row["max_score"],
            )
        )
    return grouped


def rebuild(session: Session, contest: Contest) -> StandingsPage:
    """Recompute the whole leaderboard from authoritative records, in Python."""
    grouped = authoritative_rows(session, contest.id)
    participants = session.execute(
        text(
            """
            SELECT cp.user_id, u.display_name
            FROM contest_participants cp JOIN users u ON u.id = cp.user_id
            WHERE cp.contest_id = :contest_id
            """
        ),
        {"contest_id": contest.id},
    ).mappings().all()

    # Index the cells by user first; scanning every cell per participant would be
    # participants x cells for no reason.
    by_user: dict[uuid.UUID, dict[uuid.UUID, CellResult]] = defaultdict(dict)
    for (cell_user, problem_id), rows in grouped.items():
        by_user[cell_user][problem_id] = score_cell(
            penalty_per_rejection=contest.penalty_minutes,
            contest_start=contest.starts_at,
            rows=rows,
        )

    totals = [
        aggregate_user(
            participant["user_id"],
            by_user.get(participant["user_id"], {}),
            participant["display_name"],
        )
        for participant in participants
    ]
    rows = rank_users(totals)
    return StandingsPage(
        rows=rows,
        total=len(rows),
        standings_version=contest.standings_version,
        source="rebuild",
        generated_at=datetime.now().astimezone(),
    )


def comparable(page: StandingsPage) -> list[tuple]:
    """Projection used to assert that two paths agree."""
    return [
        (
            row.rank,
            str(row.user_id),
            row.solved,
            row.points,
            row.penalty_minutes,
            row.last_solved_at.isoformat() if row.last_solved_at else None,
        )
        for row in page.rows
    ]


def verify(session: Session, contest: Contest) -> dict:
    served = serve(session, contest, limit=10_000, offset=0)
    rebuilt = rebuild(session, contest)
    naive_page = naive(session, contest, limit=10_000, offset=0)
    a, b, c = comparable(served), comparable(rebuilt), comparable(naive_page)
    return {
        "contest_id": str(contest.id),
        "standings_version": contest.standings_version,
        "rows": len(a),
        "serving_matches_rebuild": a == b,
        "serving_matches_naive": a == c,
        "serving": a,
        "rebuild": b,
        "naive": c,
    }
