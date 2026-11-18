"""Contest rules, in isolation. No database, no API, no execution."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/srv/api")

from app.domain.enums import Verdict  # noqa: E402
from app.services.scoring import (  # noqa: E402
    JudgedSubmission,
    aggregate_user,
    rank_users,
    score_cell,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def judged(minutes: int, verdict: Verdict, max_score: int = 100) -> JudgedSubmission:
    return JudgedSubmission(
        submission_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        submitted_at=START + timedelta(minutes=minutes),
        verdict=verdict,
        score=max_score if verdict is Verdict.ACCEPTED else 0,
        max_score=max_score,
    )


def cell(rows, penalty=20):
    return score_cell(penalty_per_rejection=penalty, contest_start=START, rows=rows)


class TestCellScoring:
    def test_unsolved_cell_scores_nothing(self):
        result = cell([judged(5, Verdict.WRONG_ANSWER)])
        assert (result.points, result.solved, result.penalty_minutes) == (0, False, 0)
        assert result.rejected_before_solve == 1

    def test_accepted_scores_the_pinned_max_points(self):
        result = cell([judged(7, Verdict.ACCEPTED)])
        assert result.points == 100 and result.solved is True
        assert result.penalty_minutes == 7  # elapsed minutes, no rejections

    def test_penalty_counts_only_rejections_before_the_solve(self):
        result = cell(
            [
                judged(3, Verdict.WRONG_ANSWER),
                judged(6, Verdict.RUNTIME_ERROR),
                judged(10, Verdict.ACCEPTED),
                judged(12, Verdict.WRONG_ANSWER),  # after the solve, so ignored
            ]
        )
        assert result.rejected_before_solve == 2
        assert result.penalty_minutes == 10 + 2 * 20

    def test_elapsed_minutes_floor_not_round(self):
        rows = [
            JudgedSubmission(
                submission_id=uuid.uuid4(),
                attempt_id=uuid.uuid4(),
                submitted_at=START + timedelta(seconds=119),
                verdict=Verdict.ACCEPTED,
                score=100,
                max_score=100,
            )
        ]
        assert cell(rows).penalty_minutes == 1

    def test_arrival_order_does_not_matter(self):
        rows = [judged(3, Verdict.WRONG_ANSWER), judged(10, Verdict.ACCEPTED)]
        assert cell(rows) == cell(list(reversed(rows)))

    def test_first_accepted_wins_even_if_a_later_one_exists(self):
        result = cell([judged(10, Verdict.ACCEPTED), judged(20, Verdict.ACCEPTED)])
        assert result.penalty_minutes == 10

    def test_a_compile_error_counts_as_a_rejection(self):
        result = cell([judged(2, Verdict.COMPILE_ERROR), judged(5, Verdict.ACCEPTED)])
        assert result.rejected_before_solve == 1
        assert result.penalty_minutes == 5 + 20

    def test_empty_cell_is_zero_everything(self):
        result = cell([])
        assert (result.points, result.solved, result.penalty_minutes, result.rejected_before_solve) == (
            0,
            False,
            0,
            0,
        )


class TestRanking:
    def test_more_solved_outranks_lower_penalty(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        two_solves = aggregate_user(
            uuid.UUID(int=1),
            {a: cell([judged(5, Verdict.ACCEPTED)]), b: cell([judged(9, Verdict.ACCEPTED)])},
        )
        one_fast_solve = aggregate_user(uuid.UUID(int=2), {a: cell([judged(1, Verdict.ACCEPTED)])})
        rows = rank_users([one_fast_solve, two_solves])
        assert [row.user_id for row in rows] == [two_solves.user_id, one_fast_solve.user_id]
        assert [row.rank for row in rows] == [1, 2]

    def test_equal_scores_share_a_rank_and_order_is_total(self):
        problem = uuid.uuid4()
        solved = cell([judged(5, Verdict.ACCEPTED)])
        users = [aggregate_user(uuid.UUID(int=n), {problem: solved}) for n in (3, 1, 2)]
        rows = rank_users(users)
        assert [row.rank for row in rows] == [1, 1, 1]
        # user_id is the final tiebreak, so the order is fixed
        assert [str(row.user_id) for row in rows] == sorted(str(u.user_id) for u in users)

    def test_dense_rank_skips_after_a_tie(self):
        problem = uuid.uuid4()
        solved = cell([judged(5, Verdict.ACCEPTED)])
        unsolved = cell([judged(5, Verdict.WRONG_ANSWER)])
        users = [
            aggregate_user(uuid.UUID(int=1), {problem: solved}),
            aggregate_user(uuid.UUID(int=2), {problem: solved}),
            aggregate_user(uuid.UUID(int=3), {problem: unsolved}),
        ]
        assert [row.rank for row in rank_users(users)] == [1, 1, 3]

    def test_lower_penalty_wins_at_equal_solves(self):
        problem = uuid.uuid4()
        fast = aggregate_user(uuid.UUID(int=1), {problem: cell([judged(2, Verdict.ACCEPTED)])})
        slow = aggregate_user(
            uuid.UUID(int=2),
            {problem: cell([judged(1, Verdict.WRONG_ANSWER), judged(3, Verdict.ACCEPTED)])},
        )
        rows = rank_users([slow, fast])
        assert [row.user_id for row in rows] == [fast.user_id, slow.user_id]
