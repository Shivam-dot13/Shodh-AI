"""Scenario family: recovery.

Two failure windows have to be survivable, and both are exercised with explicitly armed
failpoints rather than by hoping for a crash:

  * between committing a submission and dispatching its job
  * after judging but before the result is acknowledged
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text

from conftest import attempt_rows, cell_row, job_row, require_quiet_queue, submit, wait_for_terminal

pytestmark = pytest.mark.integration


@pytest.fixture()
def failpoint():
    """Arm a failpoint for one test and disarm it afterwards.

    The switch lives in the database, so it applies to every API process; this waits until the
    whole fleet reports it rather than assuming the process that answered is the only one.
    """
    armed: list[tuple] = []

    def settle(bundle, name: str, enabled: bool) -> None:
        deadline = time.monotonic() + 10
        agreed = 0
        while time.monotonic() < deadline and agreed < 10:
            version = bundle["organizer"].get("/version").json()
            agreed = agreed + 1 if (name in version["failpoints_armed"]) == enabled else 0
            if agreed < 10:
                time.sleep(0.05)
        assert agreed >= 10, f"failpoint {name} did not reach every API process"

    def arm(bundle, name: str, enabled: bool = True):
        response = bundle["organizer"].post("/dev/failpoints", json={"name": name, "enabled": enabled})
        assert response.status_code == 200, response.text
        settle(bundle, name, enabled)
        if enabled:
            armed.append((bundle, name))

    yield arm
    for bundle, name in armed:
        bundle["organizer"].post("/dev/failpoints", json={"name": name, "enabled": False})
        settle(bundle, name, False)


def test_a_crash_after_commit_still_leaves_a_dispatchable_job(fresh_contest, programs, failpoint, db):
    """The outbox is written in the submission transaction, so this window cannot lose work.

    The client sees a failure and does not learn the submission id. The submission and its
    queue row are nevertheless committed, the judge picks the work up, and the client retry
    with the same idempotency key finds the original submission already judged.
    """
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    key = f"crash-{uuid.uuid4().hex}"
    source = programs("accepted")

    failpoint(bundle, "crash_after_submission_commit")
    crashed = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key, expect=())
    assert crashed.status_code == 503
    assert crashed.json()["error"]["type"] == "infrastructure"

    row = db.execute(
        text(
            "SELECT id, status FROM submissions WHERE contest_id = :c AND user_id = :u AND idempotency_key = :k"
        ),
        {"c": bundle["contest_id"], "u": bundle["user_ids"]["rohan@acme.test"], "k": key},
    ).mappings().one()
    stored = attempt_rows(db, str(row["id"]))
    assert len(stored) == 1, "the attempt was committed with the submission"
    attempt_id = db.execute(
        text("SELECT id FROM judging_attempts WHERE submission_id = :s"), {"s": str(row["id"])}
    ).scalar_one()
    assert job_row(db, str(attempt_id)) is not None, "the queue row was committed too"

    failpoint(bundle, "crash_after_submission_commit", enabled=False)
    replay = submit(rohan, bundle["contest_id"], bundle["problem_id"], source, idempotency_key=key)
    assert replay.status_code == 200
    assert replay.json()["id"] == str(row["id"])
    assert replay.json()["replayed"] is True

    judged = wait_for_terminal(rohan, str(row["id"]))
    assert judged["authoritative_attempt"]["verdict"] == "ACCEPTED"
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["points"] == 100


def test_a_controllable_runner_failure_is_never_a_student_verdict(fresh_contest, programs, db):
    """A seeded program asks the runner to fail; the judge must own that failure."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs("runner_failure_controllable")
    ).json()
    final = wait_for_terminal(rohan, submission["id"])

    assert final["status"] == "JUDGE_FAILED"
    assert final["authoritative_attempt"] is None
    attempt = final["latest_attempt"]
    assert attempt["state"] == "INFRA_FAILED"
    assert attempt["verdict"] is None
    assert attempt["score"] == 0

    job = job_row(db, attempt["id"])
    assert job["state"] == "DEAD"
    assert job["delivery_count"] == job["max_deliveries"], "the delivery budget was spent, then stopped"
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell is None or (cell["points"] == 0 and cell["solved"] is False)


def test_a_sandbox_crash_is_an_infrastructure_failure(fresh_contest, programs, execution_mode):
    if execution_mode != "docker":
        pytest.skip("the in-container harness failpoint only applies to the docker execution mode")
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs("sandbox_crash_controllable")
    ).json()
    final = wait_for_terminal(rohan, submission["id"])
    assert final["status"] == "JUDGE_FAILED"
    assert final["latest_attempt"]["state"] == "INFRA_FAILED"
    assert final["latest_attempt"]["verdict"] is None


@pytest.mark.concurrency
def test_a_worker_that_dies_before_acknowledging_produces_exactly_one_verdict(
    fresh_contest, programs, manifest, clients, db
):
    """The second failure window: judged, then the worker is gone before it could report.

    Simulated exactly: a lease is taken and never acknowledged, the lease is expired, the work
    is reclaimed, and a second delivery reports the result. One verdict, one score.
    """
    require_quiet_queue()
    judge = clients(manifest["service_tokens"]["judge"], "judge")
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]

    first = judge.post("/internal/judge/leases", json={"worker_id": "worker-doomed", "capacity": 4}).json()
    held = next(h for h in first["leases"] if h["attempt_id"] == attempt_id)

    # The worker judged the program and then died. Nothing was reported.
    db.execute(
        text(
            "UPDATE judge_jobs SET lease_expires_at = now() - interval '1 second'"
            " WHERE attempt_id = :a AND state = 'LEASED'"
        ),
        {"a": attempt_id},
    )
    db.commit()
    assert judge.post("/internal/judge/reap").json()["requeued"] >= 1

    second = judge.post("/internal/judge/leases", json={"worker_id": "worker-two", "capacity": 4}).json()
    retried = next(h for h in second["leases"] if h["attempt_id"] == attempt_id)
    assert retried["lease_epoch"] > held["lease_epoch"]

    def payload(lease, status):
        return {
            "lease_epoch": lease["lease_epoch"],
            "worker_id": "w",
            "exec_mode": "test-double",
            "compile": {"ok": True, "message": ""},
            "tests": [
                {
                    "test_case_id": case["test_case_id"],
                    "status": status,
                    "time_ms": 1,
                    "memory_kb": 1024,
                    "exit_code": 0,
                    "output_bytes": 2,
                }
                for case in lease["payload"]["test_cases"]
            ],
        }

    accepted = judge.post(
        f"/internal/judge/attempts/{attempt_id}/result", json=payload(retried, "PASSED")
    )
    assert accepted.status_code == 200

    # The dead worker wakes up and reports its own result. It must be fenced out.
    zombie = judge.post(
        f"/internal/judge/attempts/{attempt_id}/result", json=payload(held, "WRONG_OUTPUT")
    )
    assert zombie.status_code == 409

    stored = attempt_rows(db, submission["id"])
    assert len(stored) == 1
    assert stored[0]["verdict"] == "ACCEPTED"
    assert db.execute(
        text("SELECT COUNT(*) FROM attempt_test_results WHERE attempt_id = :a"), {"a": attempt_id}
    ).scalar_one() == 3
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["points"] == 100 and cell["solved"] is True
    assert job_row(db, attempt_id)["state"] == "DONE"


def test_the_queue_drains_to_nothing(fresh_contest, programs, db):
    """After a burst, no job is left stuck: every one reaches DONE or DEAD."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    ids = [
        submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted") + f"\n# {n}\n").json()["id"]
        for n in range(4)
    ]
    for submission_id in ids:
        wait_for_terminal(rohan, submission_id)
    stuck = db.execute(
        text(
            """
            SELECT j.state, COUNT(*) AS n
            FROM judge_jobs j
            JOIN judging_attempts a ON a.id = j.attempt_id
            JOIN submissions s ON s.id = a.submission_id
            WHERE s.contest_id = :c AND j.state NOT IN ('DONE', 'DEAD')
            GROUP BY j.state
            """
        ),
        {"c": bundle["contest_id"]},
    ).mappings().all()
    assert stuck == [], f"jobs left unfinished: {[dict(r) for r in stuck]}"
    # The organizer-facing view is there for a human to check the same thing.
    assert "queue" in bundle["organizer"].get("/ops/queue").json()
