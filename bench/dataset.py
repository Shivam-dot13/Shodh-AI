"""Builds a labelled benchmark dataset through the public API.

Everything is created the way a real organizer and real participants would create it, and the
verdicts are applied through the judge plane. The only shortcut is *who* judges: a synthetic
judge completes attempts immediately instead of starting a container, and every attempt it
writes is labelled `exec_mode="bench-double"` so no measurement can be mistaken for one that
exercised the sandbox. Scoring, standings and all constraints are the production code paths.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

API = os.environ.get("API_BASE_URL", "http://api:8000")
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))
PROGRAMS = pathlib.Path("/srv/seed/data/programs")
PREFILL_CONCURRENCY = int(os.environ.get("PREFILL_CONCURRENCY", "12"))

VERDICT_MIX = (
    ("PASSED", 0.45),
    ("WRONG_OUTPUT", 0.30),
    ("RUNTIME_ERROR", 0.15),
    ("TIMEOUT", 0.10),
)


def client(token: str | None = None, timeout: float = 60.0) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=API, headers=headers, timeout=timeout)


@dataclass
class Dataset:
    label: str
    org_id: str
    contest_id: str
    organizer_token: str
    judge_token: str
    problems: list[dict] = field(default_factory=list)
    participants: list[dict] = field(default_factory=list)
    submissions: int = 0
    judged: int = 0

    def summary(self) -> dict:
        return {
            "label": self.label,
            "contest_id": self.contest_id,
            "participants": len(self.participants),
            "problems": len(self.problems),
            "submissions_prefilled": self.submissions,
            "verdicts_prefilled": self.judged,
            "cells": len(self.participants) * len(self.problems),
        }

    def save(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: pathlib.Path) -> Dataset:
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def _program(role: str = "accepted") -> str:
    return (PROGRAMS / "sum_two_accepted.py").read_text(encoding="utf-8")


def _suite(cases: int) -> dict:
    return {
        "label": f"bench {cases} cases",
        "cases": [
            {
                "ordinal": n,
                "input": f"{n} {n * 2}\n",
                "expected_output": f"{n * 3}\n",
                "is_sample": n == 1,
                "weight": 1,
            }
            for n in range(1, cases + 1)
        ],
    }


def build(
    *,
    participants: int,
    problems: int,
    submissions_per_cell: int,
    cases_per_problem: int = 3,
    scoring_mode: str = "ICPC",
    seed: int = 20261117,
    label: str | None = None,
) -> Dataset:
    manifest = json.loads((EVIDENCE / "seed-manifest.json").read_text(encoding="utf-8"))
    admin_token = manifest["service_tokens"]["platform_admin"]
    judge_token = manifest["service_tokens"]["judge"]
    label = label or f"bench-{participants}p-{problems}q-{submissions_per_cell}s"
    rng = random.Random(seed)
    tag = uuid.uuid4().hex[:8]

    admin = client(admin_token)
    org = admin.post("/orgs", json={"slug": f"bench-{tag}", "name": f"Bench {tag}"})
    org.raise_for_status()
    org_id = org.json()["id"]

    organizer = admin.post(
        f"/orgs/{org_id}/users",
        json={"email": f"bench-org-{tag}@bench.test", "display_name": "Bench Organizer", "role": "ORGANIZER"},
    )
    organizer.raise_for_status()
    organizer_token = organizer.json()["token"]
    organizer_client = client(organizer_token)

    now = dt.datetime.now(dt.timezone.utc)
    contest = organizer_client.post(
        "/contests",
        json={
            "slug": "bench",
            "title": f"Benchmark contest {tag}",
            "starts_at": (now - dt.timedelta(hours=1)).isoformat(),
            "ends_at": (now + dt.timedelta(days=7)).isoformat(),
            "scoring_mode": scoring_mode,
            "penalty_minutes": 20,
        },
    )
    contest.raise_for_status()
    contest_id = contest.json()["id"]
    dataset = Dataset(
        label=label,
        org_id=org_id,
        contest_id=contest_id,
        organizer_token=organizer_token,
        judge_token=judge_token,
    )

    for index in range(problems):
        response = organizer_client.post(
            f"/contests/{contest_id}/problems",
            json={
                "slug": f"q{index + 1}",
                "title": f"Benchmark problem {index + 1}",
                "ordinal": index + 1,
                "version": {
                    "statement": "Print the third multiple.",
                    "max_points": 100,
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 128,
                    "output_limit_bytes": 8192,
                    "test_suite": _suite(cases_per_problem),
                },
            },
        )
        response.raise_for_status()
        dataset.problems.append(response.json())

    for index in range(participants):
        created = admin.post(
            f"/orgs/{org_id}/users",
            json={
                "email": f"bench-{tag}-p{index}@bench.test",
                "display_name": f"Bench Player {index:04d}",
                "role": "PARTICIPANT",
            },
        )
        created.raise_for_status()
        row = created.json()
        participant = client(row["token"])
        participant.post(f"/contests/{contest_id}/participants/me").raise_for_status()
        dataset.participants.append({"id": row["id"], "email": row["email"], "token": row["token"]})
        participant.close()

    if submissions_per_cell:
        _prefill(dataset, submissions_per_cell, rng)

    admin.close()
    organizer_client.close()
    return dataset


def _prefill(dataset: Dataset, submissions_per_cell: int, rng: random.Random) -> None:
    """Fill the contest with history: real submissions, judged by a labelled synthetic judge.

    Setting up a dataset is not the thing being measured, so it runs concurrently. The verdict
    *mix* stays deterministic: each (participant, problem, attempt) draws its status from the
    seeded sequence by index, not from whatever order the requests happen to complete in.
    """
    source = _program()
    plan: list[tuple[int, dict, dict, int]] = []
    index = 0
    for participant in dataset.participants:
        for problem in dataset.problems:
            for attempt in range(submissions_per_cell):
                plan.append((index, participant, problem, attempt))
                index += 1
    statuses = {entry[0]: _pick(rng) for entry in plan}

    local = threading.local()

    def session_for(token: str) -> httpx.Client:
        cache = getattr(local, "clients", None)
        if cache is None:
            cache = local.clients = {}
        if token not in cache:
            cache[token] = client(token)
        return cache[token]

    created = 0
    lock = threading.Lock()

    def create(entry) -> None:
        nonlocal created
        order, participant, problem, attempt = entry
        response = session_for(participant["token"]).post(
            f"/contests/{dataset.contest_id}/submissions",
            json={
                "problem_id": problem["id"],
                "language": "python3",
                "source_code": f"{source}\n# {participant['email']} {problem['slug']} {attempt}\n",
            },
            headers={
                "Idempotency-Key": f"prefill-{problem['slug']}-{attempt}",
                # The plan index travels as the request id, so the synthetic judge can give this
                # submission the verdict the seeded sequence assigned to it regardless of the
                # order the concurrent requests complete in.
                "X-Request-Id": str(order),
            },
        )
        response.raise_for_status()
        with lock:
            created += 1

    with ThreadPoolExecutor(max_workers=PREFILL_CONCURRENCY) as pool:
        list(pool.map(create, plan))
    dataset.submissions += created

    # Judge everything that is queued, through the real judge plane, with a labelled double.
    judged = 0
    judge_lease = client(dataset.judge_token)

    def report(lease: dict) -> bool:
        status = statuses.get(_order_of(lease), "PASSED")
        payload = {
            "lease_epoch": lease["lease_epoch"],
            "worker_id": "bench-double",
            "exec_mode": "bench-double",
            "runner_image": "none",
            "compile": {"ok": True, "message": ""},
            "tests": [
                {
                    "test_case_id": case["test_case_id"],
                    "status": status,
                    "time_ms": 7,
                    "memory_kb": 12_000,
                    "exit_code": 0,
                    "output_bytes": 4,
                    "truncated": False,
                }
                for case in lease["payload"]["test_cases"]
            ],
            "time_ms": 7,
            "memory_kb": 12_000,
        }
        response = session_for(dataset.judge_token).post(
            f"/internal/judge/attempts/{lease['attempt_id']}/result", json=payload
        )
        return response.status_code == 200

    try:
        with ThreadPoolExecutor(max_workers=PREFILL_CONCURRENCY) as pool:
            while True:
                leases = judge_lease.post(
                    "/internal/judge/leases",
                    json={
                        "worker_id": "bench-double",
                        "capacity": 16,
                        # Stay inside this dataset: never touch work belonging to another contest.
                        "contest_id": dataset.contest_id,
                    },
                )
                leases.raise_for_status()
                held = leases.json()["leases"]
                if not held:
                    break
                judged += sum(1 for ok in pool.map(report, held) if ok)
    finally:
        judge_lease.close()
    dataset.judged += judged


def _order_of(lease: dict) -> int:
    """The submission's position in the seeded plan, carried through as its request id."""
    try:
        return int(lease["payload"].get("request_id") or -1)
    except (TypeError, ValueError):
        return -1


def _pick(rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for status, share in VERDICT_MIX:
        cumulative += share
        if roll <= cumulative:
            return status
    return VERDICT_MIX[-1][0]
