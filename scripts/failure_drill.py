"""One injected infrastructure failure, its recovery, and the final stored outcome.

    docker compose run --rm --entrypoint python tests scripts/failure_drill.py

Two drills, both deterministic:

  A. a crash after the submission commit but before the response — the window Stage 3 asks
     about on the dispatch side
  B. a runner that refuses to run, retried until the delivery budget is spent — the window on
     the judging side

For each one it shows what the client saw, what the database holds, how the system recovered,
and the correlated structured log lines from the API and the judge, found by request id.
Writes evidence/failure-drill-<timestamp>.log.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time
import uuid

import httpx
from sqlalchemy import create_engine, text

API = os.environ.get("API_BASE_URL", "http://api:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://shodh:shodh_dev_password@postgres:5432/shodh")
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))
PROGRAMS = pathlib.Path("/srv/seed/data/programs")

lines: list[str] = []


def say(text_line: str = "") -> None:
    print(text_line, flush=True)
    lines.append(text_line)


def client(token: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=60.0)


def rows(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(text(sql), params).mappings()]


def wait_terminal(session: httpx.Client, submission_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = session.get(f"/submissions/{submission_id}").json()
        if body["status"] in ("JUDGED", "JUDGE_FAILED"):
            return body
        time.sleep(0.3)
    raise SystemExit(f"timed out waiting for {submission_id}")


def settle(session: httpx.Client, name: str, enabled: bool) -> None:
    """Wait until every API process reports the failpoint in the wanted state."""
    deadline = time.monotonic() + 15
    agreed = 0
    while time.monotonic() < deadline and agreed < 10:
        armed = session.get("/version").json()["failpoints_armed"]
        agreed = agreed + 1 if (name in armed) == enabled else 0
        if agreed < 10:
            time.sleep(0.05)
    if agreed < 10:
        raise SystemExit(f"failpoint {name} did not reach every API process")


def log_trail(request_id: str, limit: int = 14) -> list[str]:
    """The structured log lines for one operation, across the API and the judge workers."""
    try:
        import docker

        docker_client = docker.from_env()
    except Exception as exc:
        return [f"(docker unavailable, cannot read container logs: {exc})"]
    found: list[tuple[str, str, str]] = []
    for container in docker_client.containers.list(all=True):
        name = container.name
        if "-api-" not in name and "-worker-" not in name:
            continue
        try:
            raw = container.logs(tail=4000).decode("utf-8", "replace")
        except Exception:
            continue
        for line in raw.splitlines():
            if request_id not in line or not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            found.append(
                (
                    record.get("ts", ""),
                    name,
                    json.dumps(
                        {
                            key: record[key]
                            for key in (
                                "level",
                                "message",
                                "request_id",
                                "submission_id",
                                "attempt_id",
                                "worker_id",
                                "lease_epoch",
                                "verdict",
                                "score",
                                "failpoint",
                                "reason",
                                "retryable",
                                "delivery_count",
                                "exec_mode",
                            )
                            if key in record
                        }
                    ),
                )
            )
    found.sort(key=lambda row: row[0])
    return [f"  {ts}  {name:28s} {payload}" for ts, name, payload in found[:limit]]


def main() -> int:
    manifest = json.loads((EVIDENCE / "seed-manifest.json").read_text(encoding="utf-8"))
    acme = manifest["organizations"]["acme-university"]
    organizer = client(acme["users"]["organizer@acme.test"]["token"])
    rohan = client(acme["users"]["rohan@acme.test"]["token"])
    rohan_id = acme["users"]["rohan@acme.test"]["id"]
    engine = create_engine(DATABASE_URL, future=True)
    version = organizer.get("/version").json()

    now = dt.datetime.now(dt.timezone.utc)
    contest = organizer.post(
        "/contests",
        json={
            "slug": f"drill-{uuid.uuid4().hex[:8]}",
            "title": "Failure drill",
            "starts_at": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at": (now + dt.timedelta(hours=2)).isoformat(),
            "scoring_mode": "ICPC",
        },
    ).json()
    problem = organizer.post(
        f"/contests/{contest['id']}/problems",
        json={
            "slug": "sum-two",
            "title": "Sum of two",
            "version": {
                "statement": "Print a + b.",
                "max_points": 100,
                "test_suite": {
                    "cases": [
                        {"ordinal": 1, "input": "2 3\n", "expected_output": "5\n", "is_sample": True},
                        {"ordinal": 2, "input": "7 8\n", "expected_output": "15\n"},
                    ]
                },
            },
        },
    ).json()
    rohan.post(f"/contests/{contest['id']}/participants/me")
    accepted = (PROGRAMS / "sum_two_accepted.py").read_text(encoding="utf-8")
    breaking = (PROGRAMS / "sum_two_runner_unavailable.py").read_text(encoding="utf-8")

    say("=" * 78)
    say("Injected infrastructure failures: recovery and final stored outcome")
    say("=" * 78)
    say(f"commit {version['commit']}   execution_mode {version['execution_mode']}")
    say(f"contest {contest['id']}   problem {problem['id']}")
    say("")

    # ---------------------------------------------------------------- drill A
    say("-" * 78)
    say("DRILL A  crash after the submission commit, before the response")
    say("-" * 78)
    key = f"drill-a-{uuid.uuid4().hex[:8]}"
    request_id = f"drill-a-{uuid.uuid4().hex[:6]}"
    organizer.post("/dev/failpoints", json={"name": "crash_after_submission_commit", "enabled": True})
    settle(organizer, "crash_after_submission_commit", True)
    say("armed crash_after_submission_commit on every API process")

    crashed = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": problem["id"], "language": "python3", "source_code": accepted},
        headers={"Idempotency-Key": key, "X-Request-Id": request_id},
    )
    say(f"client saw: HTTP {crashed.status_code}  {json.dumps(crashed.json()['error'])[:150]}")
    say("the client does not know the submission id - it never got one")

    stored = rows(
        engine,
        """
        SELECT s.id::text AS submission_id, s.status, a.id::text AS attempt_id, a.state,
               j.state AS job_state, j.delivery_count
        FROM submissions s
        JOIN judging_attempts a ON a.submission_id = s.id
        LEFT JOIN judge_jobs j ON j.attempt_id = a.id
        WHERE s.contest_id = :c AND s.idempotency_key = :k
        """,
        c=contest["id"],
        k=key,
    )
    say(f"database holds: {json.dumps(stored)}")
    say("the submission, its attempt and its queue row were committed together - nothing was lost")

    organizer.post("/dev/failpoints", json={"name": "crash_after_submission_commit", "enabled": False})
    settle(organizer, "crash_after_submission_commit", False)
    replay = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": problem["id"], "language": "python3", "source_code": accepted},
        headers={"Idempotency-Key": key, "X-Request-Id": request_id},
    )
    say(
        f"client retries with the same key: HTTP {replay.status_code} "
        f"id={replay.json()['id'][:8]} replayed={replay.json()['replayed']}"
    )
    final = wait_terminal(rohan, replay.json()["id"])
    attempt = final["authoritative_attempt"]
    say(
        f"final stored outcome: status={final['status']} verdict={attempt['verdict']} "
        f"score={attempt['score']} tests={attempt['tests_passed']}/{attempt['tests_total']} "
        f"attempts={len(final['attempts'])}"
    )
    cell = rows(
        engine,
        "SELECT points, solved, rejected_before_solve FROM problem_scores"
        " WHERE contest_id = :c AND user_id = :u AND problem_id = :p",
        c=contest["id"],
        u=rohan_id,
        p=problem["id"],
    )
    say(f"standings cell: {json.dumps(cell)}")
    say("correlated log trail:")
    for line in log_trail(request_id):
        say(line)

    # ---------------------------------------------------------------- drill B
    say("")
    say("-" * 78)
    say("DRILL B  a runner that refuses to run, retried until the budget is spent")
    say("-" * 78)
    before = organizer.get(f"/contests/{contest['id']}/leaderboard").json()
    request_id_b = f"drill-b-{uuid.uuid4().hex[:6]}"
    broken = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": problem["id"], "language": "python3", "source_code": breaking},
        headers={"Idempotency-Key": f"drill-b-{uuid.uuid4().hex[:8]}", "X-Request-Id": request_id_b},
    ).json()
    say(f"submitted a program carrying SHODH_FAILPOINT:runner_unavailable  id={broken['id'][:8]}")
    failed = wait_terminal(rohan, broken["id"])
    attempt = failed["latest_attempt"]
    say(
        f"client sees: status={failed['status']} state={attempt['state']} "
        f"verdict={attempt['verdict']} score={attempt['score']}"
    )
    job = rows(
        engine,
        "SELECT j.state, j.delivery_count, j.max_deliveries, left(j.last_error, 60) AS last_error"
        " FROM judge_jobs j WHERE j.attempt_id = :a",
        a=attempt["id"],
    )
    say(f"queue row: {json.dumps(job)}")
    say("the delivery budget was spent and then stopped; the attempt carries no verdict")
    after = organizer.get(f"/contests/{contest['id']}/leaderboard").json()
    say(
        f"leaderboard unchanged: {[ (r['display_name'], r['solved'], r['points']) for r in before['items'] ]}"
        f" -> {[ (r['display_name'], r['solved'], r['points']) for r in after['items'] ]}"
    )
    verify = organizer.get(f"/contests/{contest['id']}/standings/verify").json()
    say(
        f"consistency: serving==rebuild {verify['serving_matches_rebuild']}, "
        f"serving==naive {verify['serving_matches_naive']}"
    )
    say("correlated log trail:")
    for line in log_trail(request_id_b):
        say(line)

    say("")
    say("=" * 78)
    say("A judge failure is a judge failure: no verdict, no score change, nothing stuck.")
    say("=" * 78)

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = EVIDENCE / f"failure-drill-{stamp}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[evidence] wrote {path}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
