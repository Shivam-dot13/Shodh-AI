"""Scenario family: retries.

The same key and an equivalent request from the same actor in the same contest must return the
same logical submission; the same key with different code must conflict. Coordinated with a
barrier so the requests really do overlap, not merely follow one another.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from conftest import close_contest, submit, wait_for_terminal

pytestmark = pytest.mark.integration


def test_replay_returns_the_same_submission(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"replay-{uuid.uuid4().hex}"
    source = programs("accepted")

    first = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    second = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)

    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True
    assert first.json()["replayed"] is False


def test_replay_does_not_create_a_second_attempt_or_job(fresh_contest, programs, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"replay-{uuid.uuid4().hex}"
    source = programs("accepted")
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key).json()
    for _ in range(4):
        submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    wait_for_terminal(rohan, submission["id"])

    counts = db.execute(
        text(
            """
            SELECT (SELECT COUNT(*) FROM submissions WHERE contest_id = :c AND idempotency_key = :k) AS submissions,
                   (SELECT COUNT(*) FROM judging_attempts WHERE submission_id = :s) AS attempts,
                   (SELECT COUNT(*) FROM judge_jobs j JOIN judging_attempts a ON a.id = j.attempt_id
                     WHERE a.submission_id = :s) AS jobs
            """
        ),
        {"c": bundle["contest_id"], "k": key, "s": submission["id"]},
    ).mappings().one()
    assert dict(counts) == {"submissions": 1, "attempts": 1, "jobs": 1}


def test_same_key_with_different_code_conflicts(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"conflict-{uuid.uuid4().hex}"
    submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"), idempotency_key=key)

    conflict = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("wrong_output"),
        idempotency_key=key,
        expect=(),
    )
    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "idempotency_key_conflict"
    assert error["type"] == "business_rule"


def test_same_key_with_a_different_language_or_problem_conflicts(fresh_contest, programs):
    bundle = fresh_contest(
        problem={
            "slug": "sum-two",
            "title": "Sum",
            "version": {
                "max_points": 100,
                "test_suite": {
                    "cases": [{"ordinal": 1, "input": "2 3\n", "expected_output": "5\n", "is_sample": True}]
                },
            },
        }
    )
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"conflict-{uuid.uuid4().hex}"
    submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"), idempotency_key=key)
    other_language = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("accepted", "cpp17"),
        language="cpp17",
        idempotency_key=key,
        expect=(),
    )
    assert other_language.status_code == 409


def test_a_key_is_scoped_to_the_actor(fresh_contest, programs):
    """Two participants using the same key must get two independent submissions."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    meera = bundle["participants"]["meera@acme.test"]
    key = f"shared-{uuid.uuid4().hex}"
    source = programs("accepted")
    mine = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    theirs = submit(meera, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]


def test_a_key_is_scoped_to_the_contest(fresh_contest, programs):
    first = fresh_contest()
    second = fresh_contest()
    rohan_a = first["participants"]["rohan@acme.test"]
    rohan_b = second["participants"]["rohan@acme.test"]
    key = f"cross-{uuid.uuid4().hex}"
    source = programs("accepted")
    a = submit(rohan_a, first["contest_id"], first["problem_id"], source, idempotency_key=key)
    b = submit(rohan_b, second["contest_id"], second["problem_id"], source, idempotency_key=key)
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


def test_replay_still_enforces_authentication_and_tenancy(fresh_contest, programs, clients, manifest):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"authz-{uuid.uuid4().hex}"
    source = programs("accepted")
    submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)

    anonymous = clients(None, "anonymous")
    assert (
        anonymous.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={"problem_id": bundle["problem_id"], "language": "python3", "source_code": source},
            headers={"Idempotency-Key": key},
        ).status_code
        == 401
    )
    outsider = clients(
        manifest["organizations"]["borealis-institute"]["users"]["lin@borealis.test"]["token"], "lin"
    )
    assert (
        outsider.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={"problem_id": bundle["problem_id"], "language": "python3", "source_code": source},
            headers={"Idempotency-Key": key},
        ).status_code
        == 404
    )


def test_replay_works_after_the_contest_closes(fresh_contest, programs):
    """A retry of a submission accepted while the contest was open must still return it."""
    bundle = fresh_contest(ends_in_minutes=600)
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"late-{uuid.uuid4().hex}"
    source = programs("accepted")
    first = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key).json()

    # ends_at must stay after starts_at, so the window is closed by moving the end into the past.
    rejected = bundle["organizer"].patch(
        f"/contests/{bundle['contest_id']}",
        json={"ends_at": bundle["contest"]["starts_at"]},
    )
    assert rejected.status_code == 422
    close_contest(bundle)

    fresh = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=f"new-{key}", expect=()
    )
    assert fresh.status_code == 409 and fresh.json()["error"]["code"] == "contest_closed"

    replay = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    assert replay.status_code == 200 and replay.json()["id"] == first["id"]


def test_simultaneous_use_of_one_key_creates_exactly_one_submission(fresh_contest, programs, db):
    """Eight overlapping requests, released together by a barrier."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"race-{uuid.uuid4().hex}"
    source = programs("accepted")

    attempts = 8
    barrier = threading.Barrier(attempts)
    results: list = [None] * attempts

    def fire(index: int) -> None:
        barrier.wait(timeout=30)
        results[index] = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key, expect=()
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(r.status_code for r in results)
    bodies = [r.json() for r in results]
    assert all(status in (200, 201) for status in statuses), statuses
    assert statuses.count(201) == 1, f"exactly one creation expected, got {statuses}"
    assert len({body["id"] for body in bodies}) == 1, "all callers must see one logical submission"

    submission_id = bodies[0]["id"]
    counts = db.execute(
        text(
            """
            SELECT (SELECT COUNT(*) FROM submissions WHERE contest_id = :c AND idempotency_key = :k) AS submissions,
                   (SELECT COUNT(*) FROM judging_attempts WHERE submission_id = :s) AS attempts,
                   (SELECT COUNT(*) FROM judge_jobs j JOIN judging_attempts a ON a.id = j.attempt_id
                     WHERE a.submission_id = :s) AS jobs
            """
        ),
        {"c": bundle["contest_id"], "k": key, "s": submission_id},
    ).mappings().one()
    assert dict(counts) == {"submissions": 1, "attempts": 1, "jobs": 1}

    judged = wait_for_terminal(rohan, submission_id)
    assert judged["authoritative_attempt"]["verdict"] == "ACCEPTED"
    assert judged["authoritative_attempt"]["score"] == 100


def test_simultaneous_conflicting_keys_resolve_to_one_winner_and_conflicts(fresh_contest, programs):
    """Same key, two different programs, fired together: one wins, the rest conflict."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"race-conflict-{uuid.uuid4().hex}"
    sources = [programs("accepted"), programs("wrong_output")] * 3

    barrier = threading.Barrier(len(sources))
    results: list = [None] * len(sources)

    def fire(index: int) -> None:
        barrier.wait(timeout=30)
        results[index] = submit(
            rohan,
            bundle["contest_id"],
            bundle["problem_id"],
            sources[index],
            idempotency_key=key,
            expect=(),
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(len(sources))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert set(statuses) <= {200, 201, 409}, statuses
    created = [r for r in results if r.status_code == 201][0].json()
    for response in results:
        if response.status_code == 200:
            assert response.json()["id"] == created["id"]
        elif response.status_code == 409:
            assert response.json()["error"]["code"] == "idempotency_key_conflict"
