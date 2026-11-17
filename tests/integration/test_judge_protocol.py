"""Scenario family: competing workers, out-of-order completion, fencing.

These tests *are* the judge: they hold leases and report results over the judge plane, which is
why they need the real workers stopped (scripts/evaluate.sh does that for this phase). Races
are coordinated with barriers and with explicit lease expiry, never with sleeps.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from conftest import (
    attempt_rows,
    cell_row,
    job_row,
    require_quiet_queue,
    submit,
)

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


@pytest.fixture(autouse=True)
def _quiet_queue():
    require_quiet_queue()


@pytest.fixture()
def judge(manifest, clients):
    return clients(manifest["service_tokens"]["judge"], "judge")


def lease(judge, worker_id: str, capacity: int = 1) -> list[dict]:
    response = judge.post("/internal/judge/leases", json={"worker_id": worker_id, "capacity": capacity})
    assert response.status_code == 200, response.text
    return response.json()["leases"]


@pytest.fixture()
def lease_one(judge):
    """Claim the lease for one specific attempt.

    A claim can return work the test was not asking for yet (the queue does not know what a
    test wants), so anything extra is kept and handed out on a later call instead of being
    dropped on the floor - a dropped lease would look like a vanished worker.
    """
    parked: dict[str, dict] = {}

    def take(worker_id: str, attempt_id: str) -> dict:
        if attempt_id in parked:
            return parked.pop(attempt_id)
        for _ in range(20):
            for held in lease(judge, worker_id, capacity=1):
                parked[held["attempt_id"]] = held
            if attempt_id in parked:
                return parked.pop(attempt_id)
        raise AssertionError(f"no lease was available for attempt {attempt_id}")

    return take


def all_passed_payload(held: dict, worker_id: str, *, status: str = "PASSED") -> dict:
    return {
        "lease_epoch": held["lease_epoch"],
        "worker_id": worker_id,
        "exec_mode": "test-double",
        "runner_image": "none",
        "compile": {"ok": True, "message": ""},
        "tests": [
            {
                "test_case_id": case["test_case_id"],
                "status": status,
                "time_ms": 1,
                "memory_kb": 1024,
                "exit_code": 0,
                "output_bytes": 2,
                "truncated": False,
            }
            for case in held["payload"]["test_cases"]
        ],
        "time_ms": 1,
        "memory_kb": 1024,
    }


def report(judge, attempt_id: str, payload: dict):
    return judge.post(f"/internal/judge/attempts/{attempt_id}/result", json=payload)


def expire_lease(db, attempt_id: str) -> None:
    """Controlled fault: make the lease look abandoned, as if the worker had died.

    Deterministic by design - the alternative is waiting out a real lease, which would turn a
    correctness test into a timing test.
    """
    db.execute(
        text(
            "UPDATE judge_jobs SET lease_expires_at = now() - interval '1 second'"
            " WHERE attempt_id = :a AND state = 'LEASED'"
        ),
        {"a": attempt_id},
    )
    db.commit()


def test_payload_carries_hidden_data_only_on_the_judge_plane(fresh_contest, programs, lease_one):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    held = lease_one("worker-a", submission["latest_attempt"]["id"])

    payload = held["payload"]
    assert payload["source_code"].startswith("# Correct solution")
    assert len(payload["test_cases"]) == 3
    assert all("expected_output" in case for case in payload["test_cases"])
    assert payload["runtime_config"]["time_limit_ms"] == 2000
    assert payload["request_id"], "the originating request id travels with the job"


def test_two_competing_workers_never_get_the_same_job(fresh_contest, programs, judge, clients, manifest):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]

    judge_b = clients(manifest["service_tokens"]["judge"], "judge-b")
    barrier = threading.Barrier(2)
    claims: dict[str, list] = {}

    def claim(name: str, client) -> None:
        barrier.wait(timeout=30)
        claims[name] = lease(client, name, capacity=4)

    threads = [
        threading.Thread(target=claim, args=("worker-a", judge)),
        threading.Thread(target=claim, args=("worker-b", judge_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    holders = [name for name, held in claims.items() if any(h["attempt_id"] == attempt_id for h in held)]
    assert len(holders) == 1, f"the same job was leased twice: {claims}"


def test_a_duplicate_result_at_the_same_epoch_is_idempotent(fresh_contest, programs, judge, lease_one, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]
    held = lease_one("worker-a", attempt_id)
    payload = all_passed_payload(held, "worker-a")

    first = report(judge, attempt_id, payload)
    assert first.status_code == 200
    assert first.json()["verdict"] == "ACCEPTED"

    board_before = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    for _ in range(3):
        again = report(judge, attempt_id, payload)
        assert again.status_code == 200, again.text
        assert again.json()["verdict"] == "ACCEPTED"

    board_after = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    assert board_before["items"] == board_after["items"], "a replayed ack must not score again"
    assert board_before["standings_version"] == board_after["standings_version"]

    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["points"] == 100
    results = db.execute(
        text("SELECT COUNT(*) FROM attempt_test_results WHERE attempt_id = :a"), {"a": attempt_id}
    ).scalar_one()
    assert results == 3, "per-test rows must not be duplicated by a replay"


def test_an_expired_lease_is_reclaimed_and_the_old_holder_is_fenced_out(
    fresh_contest, programs, judge, lease_one, db
):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]

    stale = lease_one("worker-dead", attempt_id)
    expire_lease(db, attempt_id)
    reaped = judge.post("/internal/judge/reap")
    assert reaped.status_code == 200
    assert reaped.json()["requeued"] >= 1

    job = job_row(db, attempt_id)
    assert job["state"] == "READY" and job["delivery_count"] == 1

    fresh = lease_one("worker-live", attempt_id)
    assert fresh["lease_epoch"] > stale["lease_epoch"], "a reclaim must bump the fencing token"
    assert fresh["delivery_count"] == 2

    # The worker that vanished comes back and tries to report. It must be refused.
    refused = report(judge, attempt_id, all_passed_payload(stale, "worker-dead", status="WRONG_OUTPUT"))
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "stale_judge_result"

    accepted = report(judge, attempt_id, all_passed_payload(fresh, "worker-live"))
    assert accepted.status_code == 200
    assert accepted.json()["verdict"] == "ACCEPTED"

    stored = attempt_rows(db, submission["id"])
    assert len(stored) == 1 and stored[0]["verdict"] == "ACCEPTED"
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["points"] == 100, "exactly one verdict, scored once"


def test_heartbeats_keep_a_lease_and_a_stale_heartbeat_is_refused(fresh_contest, programs, judge, lease_one, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]
    held = lease_one("worker-a", attempt_id)

    beat = judge.post(
        f"/internal/judge/attempts/{attempt_id}/heartbeat", json={"lease_epoch": held["lease_epoch"]}
    )
    assert beat.status_code == 200
    assert job_row(db, attempt_id)["state"] == "LEASED"
    assert attempt_rows(db, submission["id"])[0]["state"] == "RUNNING"

    stale = judge.post(
        f"/internal/judge/attempts/{attempt_id}/heartbeat", json={"lease_epoch": held["lease_epoch"] - 1}
    )
    assert stale.status_code == 409


def test_out_of_order_completion_lands_in_the_right_final_state(fresh_contest, programs, judge, lease_one, db):
    """Two submissions, completed in reverse order, with the earlier one accepted."""
    bundle = fresh_contest(penalty_minutes=20)
    rohan = bundle["participants"]["rohan@acme.test"]
    first = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("wrong_output")).json()
    second = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted") + "\n# second\n"
    ).json()

    held_first = lease_one("worker-a", first["latest_attempt"]["id"])
    held_second = lease_one("worker-b", second["latest_attempt"]["id"])

    # The later submission finishes first.
    assert report(judge, second["latest_attempt"]["id"], all_passed_payload(held_second, "worker-b")).status_code == 200
    assert (
        report(
            judge,
            first["latest_attempt"]["id"],
            all_passed_payload(held_first, "worker-a", status="WRONG_OUTPUT"),
        ).status_code
        == 200
    )

    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert cell["solved"] is True
    assert cell["rejected_before_solve"] == 1, "the earlier rejection counts even though it arrived last"
    assert str(cell["authoritative_attempt_id"]) == held_second["attempt_id"]

    report_body = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/standings/verify").json()
    assert report_body["serving_matches_rebuild"] is True
    assert report_body["serving_matches_naive"] is True


def test_a_late_result_from_a_replaced_attempt_does_not_move_the_standings(
    fresh_contest, programs, judge, lease_one, db
):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_one = submission["latest_attempt"]["id"]

    held_one = lease_one("worker-slow", attempt_one)  # worker-slow is still judging...

    rejudged = bundle["organizer"].post(
        f"/submissions/{submission['id']}/rejudge",
        json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
    )
    assert rejudged.status_code == 202
    attempt_two = rejudged.json()["id"]
    assert job_row(db, attempt_one)["state"] == "DONE", "a superseded job must not be redelivered"

    held_two = lease_one("worker-fast", attempt_two)
    assert report(judge, attempt_two, all_passed_payload(held_two, "worker-fast")).status_code == 200

    before = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()["items"]

    # The replaced attempt finally reports a different answer.
    late = report(judge, attempt_one, all_passed_payload(held_one, "worker-slow", status="WRONG_OUTPUT"))
    assert late.status_code == 200, "the result is accepted into history"

    stored = attempt_rows(db, submission["id"])
    assert len(stored) == 2
    assert stored[0]["verdict"] == "WRONG_ANSWER" and stored[1]["verdict"] == "ACCEPTED"

    after = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()["items"]
    assert before == after, "a late result from a replaced attempt must not change standings"
    cell = cell_row(db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"])
    assert str(cell["authoritative_attempt_id"]) == attempt_two


def test_retries_are_bounded_and_end_as_an_infrastructure_failure(fresh_contest, programs, judge, lease_one, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    good = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    held_good = lease_one("worker-a", good["latest_attempt"]["id"])
    report(judge, good["latest_attempt"]["id"], all_passed_payload(held_good, "worker-a"))
    scored_before = cell_row(
        db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"]
    )

    rejudged = bundle["organizer"].post(
        f"/submissions/{good['id']}/rejudge",
        json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
    ).json()
    attempt_id = rejudged["id"]

    deliveries = 0
    while True:
        held = lease_one(f"worker-flaky-{deliveries}", attempt_id)
        failed = judge.post(
            f"/internal/judge/attempts/{attempt_id}/infra-failure",
            json={
                "lease_epoch": held["lease_epoch"],
                "worker_id": f"worker-flaky-{deliveries}",
                "reason": "runner unavailable (test)",
                "retryable": True,
            },
        )
        assert failed.status_code == 200, failed.text
        deliveries += 1
        if not failed.json()["requeued"]:
            break
        # Skip the exponential backoff rather than sleep through it: the point of the test is
        # the delivery budget, not the wait.
        db.execute(
            text("UPDATE judge_jobs SET available_at = now() WHERE attempt_id = :a"), {"a": attempt_id}
        )
        db.commit()
        assert deliveries <= 5, "retries are not bounded"

    job = job_row(db, attempt_id)
    assert job["state"] == "DEAD"
    assert job["delivery_count"] == job["max_deliveries"] == 3
    stored = attempt_rows(db, good["id"])
    assert stored[1]["state"] == "INFRA_FAILED" and stored[1]["verdict"] is None

    detail = bundle["organizer"].get(f"/submissions/{good['id']}").json()
    assert detail["status"] == "JUDGED", "an earlier verdict still stands"
    assert detail["authoritative_attempt"]["attempt_no"] == 1
    scored_after = cell_row(
        db, bundle["contest_id"], bundle["user_ids"]["rohan@acme.test"], bundle["problem_id"]
    )
    assert scored_after["points"] == scored_before["points"] == 100
    assert scored_after["solved"] is True


def test_a_result_outside_the_pinned_suite_is_refused(fresh_contest, programs, judge, lease_one):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]
    held = lease_one("worker-a", attempt_id)

    foreign = all_passed_payload(held, "worker-a")
    foreign["tests"][0]["test_case_id"] = str(uuid.uuid4())
    assert report(judge, attempt_id, foreign).status_code == 422

    short = all_passed_payload(held, "worker-a")
    short["tests"] = short["tests"][:1]
    assert report(judge, attempt_id, short).status_code == 422

    duplicated = all_passed_payload(held, "worker-a")
    duplicated["tests"][1]["test_case_id"] = duplicated["tests"][0]["test_case_id"]
    assert report(judge, attempt_id, duplicated).status_code == 422

    # After three refusals the attempt is still judgeable at the same epoch.
    assert report(judge, attempt_id, all_passed_payload(held, "worker-a")).status_code == 200


def test_a_completed_attempt_cannot_be_rewritten(fresh_contest, programs, judge, lease_one, db):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    attempt_id = submission["latest_attempt"]["id"]
    held = lease_one("worker-a", attempt_id)
    assert report(judge, attempt_id, all_passed_payload(held, "worker-a")).status_code == 200

    forged = all_passed_payload(held, "worker-b", status="WRONG_OUTPUT")
    forged["lease_epoch"] = held["lease_epoch"] + 7
    assert report(judge, attempt_id, forged).status_code == 409

    failure = judge.post(
        f"/internal/judge/attempts/{attempt_id}/infra-failure",
        json={
            "lease_epoch": held["lease_epoch"],
            "worker_id": "worker-b",
            "reason": "too late",
            "retryable": True,
        },
    )
    assert failure.status_code == 409
    assert attempt_rows(db, submission["id"])[0]["verdict"] == "ACCEPTED"
