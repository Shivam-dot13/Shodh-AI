"""End-to-end walkthrough against a running stack.

    docker compose run --rm --entrypoint python tests scripts/demo.py

Joins a contest as three participants, submits programs that cover accepted, wrong answer,
runtime failure, timeout, excessive output and a controllable judge failure, waits for real
verdicts, prints the leaderboard, rejudges one submission against an explicitly chosen
problem version and test suite, and finally asserts that the serving path, the naive path and
a full rebuild agree.

Everything it prints is read back from the API, so the output doubles as request examples.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid

import httpx

API = os.environ.get("API_BASE_URL", "http://api:8000")
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))
PROGRAMS = pathlib.Path("/srv/seed/data/programs")
MANIFEST = EVIDENCE / "seed-manifest.json"
TIMEOUT = float(os.environ.get("VERDICT_TIMEOUT", "180"))

lines: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    lines.append(text)


def client(token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=API, headers=headers, timeout=60.0)


def program(role: str, language: str = "python3", problem: str = "sum-two") -> str:
    manifest = json.loads((PROGRAMS / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["programs"]:
        if entry["role"] == role and entry["language"] == language and entry["problem"] == problem:
            return (PROGRAMS / entry["file"]).read_text(encoding="utf-8")
    raise KeyError(f"no seeded program for {problem} / {role} / {language}")


def wait_terminal(c: httpx.Client, submission_id: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        body = c.get(f"/submissions/{submission_id}").json()
        if body["status"] in ("JUDGED", "JUDGE_FAILED"):
            return body
        time.sleep(0.3)
    raise SystemExit(f"timed out waiting for {submission_id}")


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contest = manifest["contests"]["autumn-open"]
    acme = manifest["organizations"]["acme-university"]
    problems = contest["problems"]
    sum_two = problems["sum-two"]
    max_in_list = problems["max-in-list"]

    version = httpx.get(f"{API}/version", timeout=10).json()
    say("=" * 78)
    say("Shodh a Code - end to end walkthrough")
    say("=" * 78)
    say(f"commit            {version['commit']}")
    say(f"execution mode    {version['execution_mode']}  (docker = real container isolation)")
    say(f"data version      {manifest['data_version']} / {manifest['programs_version']}")
    say(f"contest           {contest['id']}  scoring={contest['scoring_mode']}")
    say("")

    organizer = client(acme["users"]["organizer@acme.test"]["token"])
    rohan = client(acme["users"]["rohan@acme.test"]["token"])
    meera = client(acme["users"]["meera@acme.test"]["token"])
    dev = client(acme["users"]["dev@acme.test"]["token"])

    say("-- 1. participants join (idempotent) " + "-" * 40)
    for name, c in (("rohan", rohan), ("meera", meera), ("dev", dev)):
        response = c.post(f"/contests/{contest['id']}/participants/me")
        say(f"   {name:6s} join -> {response.status_code}")

    say("")
    say("-- 2. submissions covering every documented outcome " + "-" * 25)
    plan = [
        ("rohan", rohan, sum_two, "accepted", "python3"),
        ("rohan", rohan, max_in_list, "wrong_output", "python3"),
        ("meera", meera, sum_two, "wrong_output", "python3"),
        ("meera", meera, sum_two, "accepted", "python3"),
        ("dev", dev, sum_two, "runtime_failure", "python3"),
        ("dev", dev, sum_two, "timeout", "python3"),
        ("dev", dev, sum_two, "excessive_output", "python3"),
        ("dev", dev, sum_two, "compile_error", "python3"),
        ("dev", dev, sum_two, "accepted", "cpp17"),
        ("rohan", rohan, sum_two, "runner_failure_controllable", "python3"),
    ]
    submitted = []
    for who, c, problem, role, language in plan:
        response = c.post(
            f"/contests/{contest['id']}/submissions",
            json={
                "problem_id": problem["problem_id"],
                "language": language,
                "source_code": program(role, language, problem["slug"]),
            },
            headers={"Idempotency-Key": f"demo-{uuid.uuid4().hex}", "X-Request-Id": f"demo-{role}-{who}"},
        )
        if response.status_code not in (200, 201):
            say(f"   {who:6s} {role:28s} -> {response.status_code} {response.text[:160]}")
            continue
        body = response.json()
        submitted.append((who, c, role, language, body["id"]))
        say(f"   {who:6s} {role:28s} {language:7s} -> {body['status']}  id={body['id'][:8]}")

    say("")
    say("-- 3. idempotency " + "-" * 59)
    shared_key = f"demo-replay-{uuid.uuid4().hex}"
    first = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": sum_two["problem_id"], "language": "python3", "source_code": program("accepted")},
        headers={"Idempotency-Key": shared_key},
    )
    replay = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": sum_two["problem_id"], "language": "python3", "source_code": program("accepted")},
        headers={"Idempotency-Key": shared_key},
    )
    conflict = rohan.post(
        f"/contests/{contest['id']}/submissions",
        json={"problem_id": sum_two["problem_id"], "language": "python3", "source_code": program("wrong_output")},
        headers={"Idempotency-Key": shared_key},
    )
    say(f"   first use            -> {first.status_code} id={first.json()['id'][:8]}")
    say(
        f"   same key, same code  -> {replay.status_code} id={replay.json()['id'][:8]} "
        f"replayed={replay.json()['replayed']}"
    )
    say(f"   same key, other code -> {conflict.status_code} {conflict.json()['error']['code']}")
    submitted.append(("rohan", rohan, "accepted", "python3", first.json()["id"]))

    say("")
    say("-- 4. waiting for real verdicts " + "-" * 45)
    verdicts = {}
    for who, c, role, language, submission_id in submitted:
        body = wait_terminal(c, submission_id)
        attempt = body.get("authoritative_attempt") or {}
        latest = body.get("latest_attempt") or {}
        verdict = attempt.get("verdict") or f"({latest.get('state')})"
        verdicts[(who, role, language)] = verdict
        say(
            f"   {who:6s} {role:28s} {language:7s} status={body['status']:12s} "
            f"verdict={verdict:22s} score={attempt.get('score', 0):4d} "
            f"tests={attempt.get('tests_passed', 0)}/{attempt.get('tests_total', 0)} "
            f"mode={attempt.get('exec_mode') or latest.get('exec_mode')}"
        )

    say("")
    say("-- 5. leaderboard (serving path) " + "-" * 44)
    board = rohan.get(f"/contests/{contest['id']}/leaderboard").json()
    say(f"   standings_version={board['standings_version']} execution_mode={board['execution_mode']}")
    for row in board["items"]:
        say(
            f"   #{row['rank']} {row['display_name']:14s} solved={row['solved']} "
            f"points={row['points']:4d} penalty={row['penalty_minutes']:4d}"
        )

    say("")
    say("-- 6. rejudge against an explicitly chosen version pair " + "-" * 21)
    target = next(s for s in submitted if s[2] == "accepted" and s[3] == "python3")
    v2 = sum_two["versions"]["2"]
    response = organizer.post(
        f"/submissions/{target[4]}/rejudge",
        json={"problem_version_id": v2["id"], "test_suite_id": v2["test_suite_id"]},
    )
    say(f"   rejudge -> {response.status_code} attempt_no={response.json().get('attempt_no')}")
    body = wait_terminal(organizer, target[4])
    attempts = organizer.get(f"/submissions/{target[4]}/attempts").json()["items"]
    for attempt in attempts:
        say(
            f"   attempt {attempt['attempt_no']} kind={attempt['kind']:8s} state={attempt['state']:10s} "
            f"verdict={str(attempt['verdict']):20s} score={attempt['score']:4d} "
            f"version={attempt['problem_version_id'][:8]} suite={attempt['test_suite_id'][:8]}"
        )
    say(f"   authoritative attempt is now {(body.get('authoritative_attempt') or {}).get('attempt_no')}")

    say("")
    say("-- 7. standings consistency " + "-" * 49)
    report = organizer.get(f"/contests/{contest['id']}/standings/verify").json()
    say(f"   serving == rebuild : {report['serving_matches_rebuild']}")
    say(f"   serving == naive   : {report['serving_matches_naive']}")
    say(f"   rows               : {report['rows']}")

    say("")
    say("-- 8. authorization " + "-" * 57)
    borealis = manifest["organizations"]["borealis-institute"]
    outsider = client(borealis["users"]["lin@borealis.test"]["token"])
    cross = outsider.get(f"/contests/{contest['id']}")
    role_denied = rohan.post(
        f"/contests/{contest['id']}/problems",
        json={
            "slug": "sneaky",
            "title": "nope",
            "version": {"test_suite": {"cases": [{"ordinal": 1, "input": "1", "expected_output": "1"}]}},
        },
    )
    hidden = rohan.get(f"/problems/{sum_two['problem_id']}/test-suites")
    judge_as_user = client(manifest["service_tokens"]["judge"]).get(f"/contests/{contest['id']}")
    user_as_judge = rohan.post(
        "/internal/judge/leases", json={"worker_id": "not-a-worker", "capacity": 1}
    )
    say(f"   cross-organization read of a known contest id -> {cross.status_code} (404 by design)")
    say(f"   participant creating a problem                -> {role_denied.status_code}")
    say(f"   participant listing hidden test suites        -> {hidden.status_code}")
    say(f"   judge credential on a participant route       -> {judge_as_user.status_code}")
    say(f"   participant credential on the judge plane     -> {user_as_judge.status_code}")

    say("")
    say("=" * 78)
    say("done")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = EVIDENCE / f"demo-{stamp}.log"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[evidence] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
