"""Scenario family: normal flow.

Join, submit, judge, observe the score and the ranking - against the running stack, in whatever
execution mode it is configured for, with the real workers doing the judging.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import (
    HIDDEN_INPUT_MARKERS,
    HIDDEN_OUTPUT_MARKERS,
    attempt_rows,
    cell_row,
    submit,
    wait_for_terminal,
)

pytestmark = pytest.mark.integration


def test_submission_returns_an_identifier_before_judging_finishes(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    response = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"))

    body = response.json()
    assert response.status_code == 201
    assert response.headers["Location"] == f"/submissions/{body['id']}"
    # Accepted and queued, not judged: the judge has not run yet.
    assert body["status"] in ("QUEUED", "RUNNING")
    assert body["authoritative_attempt"] is None
    assert body["latest_attempt"]["state"] in ("PENDING", "DISPATCHED", "RUNNING")

    final = wait_for_terminal(rohan, body["id"])
    assert final["status"] == "JUDGED"
    assert final["authoritative_attempt"]["verdict"] == "ACCEPTED"


def test_accepted_submission_scores_and_ranks(fresh_contest, programs, db, execution_mode):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    meera = bundle["participants"]["meera@acme.test"]

    wrong = submit(meera, bundle["contest_id"], bundle["problem_id"], programs("wrong_output")).json()
    good = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(meera, wrong["id"])
    judged = wait_for_terminal(rohan, good["id"])

    attempt = judged["authoritative_attempt"]
    assert attempt["verdict"] == "ACCEPTED"
    assert attempt["score"] == 100 == attempt["max_score"]
    assert attempt["tests_passed"] == attempt["tests_total"] == 3
    assert attempt["exec_mode"] == execution_mode

    # Stored records, not just the HTTP response.
    stored = attempt_rows(db, good["id"])
    assert [row["state"] for row in stored] == ["COMPLETED"]
    assert stored[0]["verdict"] == "ACCEPTED"
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is True and cell["points"] == 100
    assert str(cell["authoritative_attempt_id"]) == attempt["id"]

    board = rohan.get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    ranked = {row["display_name"]: row for row in board["items"]}
    assert ranked["Rohan Kumar"]["rank"] == 1
    assert ranked["Rohan Kumar"]["solved"] == 1
    assert ranked["Meera Iyer"]["solved"] == 0
    assert ranked["Meera Iyer"]["rank"] == 2
    assert board["execution_mode"] == execution_mode


def test_per_test_results_are_visible_without_leaking_hidden_data(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs("wrong_output")
    ).json()
    wait_for_terminal(rohan, submission["id"])

    detail = rohan.get(f"/submissions/{submission['id']}").json()
    assert len(detail["test_results"]) == 3
    serialized = str(detail)
    for row in detail["test_results"]:
        assert set(row) == {"ordinal", "is_sample", "status", "time_ms", "memory_kb", "output_bytes", "truncated"}
    # Hidden inputs and expected outputs appear nowhere in a participant response.
    assert "expected_output" not in serialized
    for marker in HIDDEN_INPUT_MARKERS + HIDDEN_OUTPUT_MARKERS:
        assert marker not in serialized


def test_rejected_attempt_adds_penalty_only_after_a_solve(fresh_contest, programs, db):
    bundle = fresh_contest(penalty_minutes=20)
    rohan = bundle["participants"]["rohan@acme.test"]
    first = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("wrong_output")).json()
    wait_for_terminal(rohan, first["id"])

    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is False and cell["penalty_minutes"] == 0 and cell["rejected_before_solve"] == 1

    second = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, second["id"])
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is True
    assert cell["rejected_before_solve"] == 1
    # elapsed minutes (small, the contest opened 5 minutes ago) plus one 20 minute penalty
    assert 20 <= cell["penalty_minutes"] <= 30


def test_standings_version_advances_with_every_verdict(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    before = rohan.get(f"/contests/{bundle['contest_id']}/leaderboard").json()["standings_version"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, submission["id"])
    after = rohan.get(f"/contests/{bundle['contest_id']}/leaderboard")
    assert after.json()["standings_version"] > before
    # The tag identifies the representation: version, source and the page requested.
    assert str(after.json()["standings_version"]) in after.headers["ETag"]
    assert "standings" in after.headers["ETag"]

    cached = rohan.get(
        f"/contests/{bundle['contest_id']}/leaderboard", headers={"If-None-Match": after.headers["ETag"]}
    )
    assert cached.status_code == 304


def test_several_worker_processes_share_the_queue(fresh_contest, programs, db, execution_mode):
    """At least two independent judge processes must take part against the same store."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    ids = [
        submit(
            rohan,
            bundle["contest_id"],
            bundle["problem_id"],
            programs("accepted") + f"\n# {n}\n",
        ).json()["id"]
        for n in range(6)
    ]
    for submission_id in ids:
        wait_for_terminal(rohan, submission_id)

    workers = db.execute(
        text(
            """
            SELECT DISTINCT a.worker_id
            FROM judging_attempts a JOIN submissions s ON s.id = a.submission_id
            WHERE s.contest_id = :c AND a.worker_id IS NOT NULL
            """
        ),
        {"c": bundle["contest_id"]},
    ).scalars().all()
    if len(workers) < 2:
        pytest.skip(f"only one judge process was running: {workers}")
    assert len(workers) >= 2, workers


def test_contest_counters_agree_with_the_leaderboard(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, submission["id"])

    stats = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/stats").json()
    board = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    assert stats["participants"] == board["page"]["total"] == 2
    assert stats["submissions"] == 1
    assert stats["standings_version"] == board["standings_version"]
