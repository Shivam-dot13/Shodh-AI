"""Contest rules, in isolation. No database, no API, no execution."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/srv/api")

from app.domain.enums import ScoringMode, Verdict  # noqa: E402
from app.services.scoring import (  # noqa: E402
    JudgedSubmission,
    aggregate_user,
    rank_users,
    score_cell,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def judged(minutes: int, verdict: Verdict, score: int = 0, max_score: int = 100) -> JudgedSubmission:
    return JudgedSubmission(
        submission_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        submitted_at=START + timedelta(minutes=minutes),
        verdict=verdict,
        score=score,
        max_score=max_score,
    )


def icpc(rows, penalty=20):
    return score_cell(
        mode=ScoringMode.ICPC, penalty_per_rejection=penalty, contest_start=START, rows=rows
    )


def points(rows):
    return score_cell(mode=ScoringMode.POINTS, penalty_per_rejection=0, contest_start=START, rows=rows)


class TestIcpc:
    def test_unsolved_cell_scores_nothing(self):
        cell = icpc([judged(5, Verdict.WRONG_ANSWER)])
        assert (cell.points, cell.solved, cell.penalty_minutes) == (0, False, 0)
        assert cell.rejected_before_solve == 1

    def test_accepted_scores_the_pinned_max_points(self):
        cell = icpc([judged(7, Verdict.ACCEPTED, score=100, max_score=100)])
        assert cell.points == 100 and cell.solved is True
        assert cell.penalty_minutes == 7  # elapsed minutes, no rejections

    def test_penalty_counts_only_rejections_before_the_solve(self):
        cell = icpc(
            [
                judged(3, Verdict.WRONG_ANSWER),
                judged(6, Verdict.RUNTIME_ERROR),
                judged(10, Verdict.ACCEPTED, max_score=100),
                judged(12, Verdict.WRONG_ANSWER),  # after the solve: ignored
            ]
        )
        assert cell.rejected_before_solve == 2
        assert cell.penalty_minutes == 10 + 2 * 20

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
        assert icpc(rows).penalty_minutes == 1

    def test_arrival_order_does_not_matter(self):
        rows = [judged(3, Verdict.WRONG_ANSWER), judged(10, Verdict.ACCEPTED, max_score=100)]
        assert icpc(rows) == icpc(list(reversed(rows)))

    def test_first_accepted_wins_even_if_a_later_one_exists(self):
        cell = icpc(
            [judged(10, Verdict.ACCEPTED, max_score=100), judged(20, Verdict.ACCEPTED, max_score=100)]
        )
        assert cell.penalty_minutes == 10


class TestPoints:
    def test_best_score_wins(self):
        cell = points([judged(2, Verdict.WRONG_ANSWER, score=40), judged(5, Verdict.WRONG_ANSWER, score=60)])
        assert cell.points == 60 and cell.solved is False

    def test_earliest_submission_wins_a_tie(self):
        early = judged(2, Verdict.WRONG_ANSWER, score=60)
        late = judged(9, Verdict.WRONG_ANSWER, score=60)
        cell = points([late, early])
        assert cell.submission_id == early.submission_id

    def test_no_penalty_in_points_mode(self):
        cell = points([judged(2, Verdict.WRONG_ANSWER, score=10), judged(4, Verdict.ACCEPTED, score=100)])
        assert cell.penalty_minutes == 0 and cell.points == 100 and cell.solved is True


class TestRanking:
    def _user(self, mode, cells):
        return aggregate_user(uuid.uuid4(), cells)

    def test_more_solved_outranks_lower_penalty(self):
        a_problem, b_problem = uuid.uuid4(), uuid.uuid4()
        two_solves = aggregate_user(
            uuid.UUID(int=1),
            {
                a_problem: icpc([judged(5, Verdict.ACCEPTED, max_score=100)]),
                b_problem: icpc([judged(9, Verdict.ACCEPTED, max_score=100)]),
            },
        )
        one_fast_solve = aggregate_user(
            uuid.UUID(int=2), {a_problem: icpc([judged(1, Verdict.ACCEPTED, max_score=100)])}
        )
        rows = rank_users(ScoringMode.ICPC, [one_fast_solve, two_solves])
        assert [row.user_id for row in rows] == [two_solves.user_id, one_fast_solve.user_id]
        assert [row.rank for row in rows] == [1, 2]

    def test_equal_scores_share_a_rank_and_order_is_total(self):
        problem = uuid.uuid4()
        cell = icpc([judged(5, Verdict.ACCEPTED, max_score=100)])
        users = [aggregate_user(uuid.UUID(int=n), {problem: cell}) for n in (3, 1, 2)]
        rows = rank_users(ScoringMode.ICPC, users)
        assert [row.rank for row in rows] == [1, 1, 1]
        # final tiebreak is the user id, so the order is deterministic
        assert [str(row.user_id) for row in rows] == sorted(str(u.user_id) for u in users)

    def test_dense_rank_skips_after_a_tie(self):
        problem = uuid.uuid4()
        solved = icpc([judged(5, Verdict.ACCEPTED, max_score=100)])
        unsolved = icpc([judged(5, Verdict.WRONG_ANSWER)])
        users = [
            aggregate_user(uuid.UUID(int=1), {problem: solved}),
            aggregate_user(uuid.UUID(int=2), {problem: solved}),
            aggregate_user(uuid.UUID(int=3), {problem: unsolved}),
        ]
        assert [row.rank for row in rank_users(ScoringMode.ICPC, users)] == [1, 1, 3]

    def test_points_mode_ranks_on_points(self):
        problem = uuid.uuid4()
        rich = aggregate_user(uuid.UUID(int=1), {problem: points([judged(1, Verdict.WRONG_ANSWER, score=90)])})
        poor = aggregate_user(uuid.UUID(int=2), {problem: points([judged(1, Verdict.ACCEPTED, score=50)])})
        rows = rank_users(ScoringMode.POINTS, [poor, rich])
        assert [row.user_id for row in rows] == [rich.user_id, poor.user_id]


def test_empty_cell_is_zero_everything():
    cell = icpc([])
    assert (cell.points, cell.solved, cell.penalty_minutes, cell.rejected_before_solve) == (0, False, 0, 0)
