"""Scenario family: rejudging.

A rejudge runs an existing submission against an explicitly chosen problem version and test
suite. Earlier attempts are preserved, the newest completed attempt is authoritative, and
publishing a new problem version never rewrites history.
"""
from __future__ import annotations

import uuid

import pytest

from conftest import attempt_rows, cell_row, submit, wait_for_terminal

pytestmark = pytest.mark.integration

HARDER_SUITE = {
    "label": "adds a case the first version never had",
    "cases": [
        {"ordinal": 1, "input": "2 3\n", "expected_output": "5\n", "is_sample": True, "weight": 1},
        {"ordinal": 2, "input": "717171 282828\n", "expected_output": "999999\n", "weight": 1},
        {"ordinal": 3, "input": "123456 654321\n", "expected_output": "777777\n", "weight": 1},
        {"ordinal": 4, "input": "-9 -9\n", "expected_output": "-18\n", "weight": 1},
    ],
}


def publish_version(bundle, *, max_points=150, publish=False, suite=None):
    response = bundle["organizer"].post(
        f"/problems/{bundle['problem_id']}/versions",
        json={
            "statement": "Print a + b. Now with negative numbers.",
            "time_limit_ms": 3000,
            "memory_limit_mb": 160,
            "output_limit_bytes": 8192,
            "max_points": max_points,
            "compare_mode": "TRIM_TRAILING_WS",
            "test_suite": suite or HARDER_SUITE,
            "publish": publish,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rejudge_adds_an_attempt_and_keeps_the_old_one(fresh_contest, programs, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    first = wait_for_terminal(rohan, submission["id"])
    assert first["authoritative_attempt"]["score"] == 100

    version_two = publish_version(bundle, max_points=150)
    accepted = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={
            "problem_version_id": version_two["id"],
            "test_suite_id": version_two["default_test_suite_id"],
        },
    )
    assert accepted.status_code == 202
    assert accepted.json()["attempt_no"] == 2
    assert accepted.json()["kind"] == "REJUDGE"

    final = wait_for_terminal(rohan, submission["id"])
    stored = attempt_rows(db, submission["id"])
    assert len(stored) == 2, "the first attempt must still be there"
    assert [row["attempt_no"] for row in stored] == [1, 2]
    assert stored[0]["verdict"] == "ACCEPTED" and stored[0]["score"] == 100
    assert stored[1]["verdict"] == "ACCEPTED" and stored[1]["score"] == 150
    # Each attempt names the exact pair it ran against.
    assert str(stored[0]["problem_version_id"]) == bundle["version_id"]
    assert str(stored[1]["problem_version_id"]) == version_two["id"]

    assert final["authoritative_attempt"]["attempt_no"] == 2
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["points"] == 150, "standings follow the newest completed attempt"


def test_publishing_a_new_version_does_not_change_a_historical_verdict(fresh_contest, programs, db):
    """A partial solution stays accepted on the version it was judged against."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("partial_output")).json()
    judged = wait_for_terminal(rohan, submission["id"])
    # partial_output prints 0 for negative sums; version one has no negative case, so it passes.
    assert judged["authoritative_attempt"]["verdict"] == "ACCEPTED"
    before = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()

    publish_version(bundle, max_points=150, publish=True)

    after = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    assert [(row["user_id"], row["points"], row["solved"]) for row in after["items"]] == [
        (row["user_id"], row["points"], row["solved"]) for row in before["items"]
    ]
    unchanged = bundle["organizer"].get(f"/submissions/{submission['id']}").json()
    assert unchanged["authoritative_attempt"]["verdict"] == "ACCEPTED"
    assert str(attempt_rows(db, submission["id"])[0]["problem_version_id"]) == bundle["version_id"]


def test_rejudging_against_a_harder_suite_can_lower_the_verdict(fresh_contest, programs, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("partial_output")).json()
    wait_for_terminal(rohan, submission["id"])
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is True

    version_two = publish_version(bundle, max_points=150, publish=True)
    response = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={
            "problem_version_id": version_two["id"],
            "test_suite_id": version_two["default_test_suite_id"],
        },
    )
    assert response.status_code == 202
    final = wait_for_terminal(rohan, submission["id"])
    assert final["authoritative_attempt"]["verdict"] == "WRONG_ANSWER"
    assert final["authoritative_attempt"]["tests_passed"] == 3
    assert final["authoritative_attempt"]["tests_total"] == 4

    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is False, "the newest completed attempt is authoritative"
    assert cell["points"] == 0, "ICPC mode scores nothing for an unsolved problem"


def test_rejudge_can_reuse_an_older_version_pair(fresh_contest, programs, db):
    """The organizer chooses the pair explicitly; it does not have to be the current one."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("partial_output")).json()
    wait_for_terminal(rohan, submission["id"])
    version_two = publish_version(bundle, max_points=150, publish=True)
    bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={
            "problem_version_id": version_two["id"],
            "test_suite_id": version_two["default_test_suite_id"],
        },
    )
    wait_for_terminal(rohan, submission["id"])

    back_to_v1 = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
    )
    assert back_to_v1.status_code == 202 and back_to_v1.json()["attempt_no"] == 3
    final = wait_for_terminal(rohan, submission["id"])
    assert final["authoritative_attempt"]["attempt_no"] == 3
    assert final["authoritative_attempt"]["verdict"] == "ACCEPTED"
    assert len(attempt_rows(db, submission["id"])) == 3


def test_rejudge_rejects_a_pair_from_another_problem(fresh_contest, programs):
    bundle = fresh_contest()
    other = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()

    mismatched = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={"problem_version_id": other["version_id"], "test_suite_id": other["suite_id"]},
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["type"] == "validation"

    unknown = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={"problem_version_id": str(uuid.uuid4()), "test_suite_id": bundle["suite_id"]},
    )
    assert unknown.status_code == 422


def test_rejudge_is_audited(fresh_contest, programs, db):
    from sqlalchemy import text

    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, submission["id"])
    bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
        headers={"X-Request-Id": "audit-me"},
    )
    row = db.execute(
        text(
            "SELECT action, target_id, request_id, detail FROM audit_log"
            " WHERE target_id = :s ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": submission["id"]},
    ).mappings().one()
    assert row["action"] == "rejudge"
    assert row["request_id"] == "audit-me"
    assert row["detail"]["attempt_no"] == 2
