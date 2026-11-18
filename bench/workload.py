"""Repeatable concurrent workload: overlapping submissions and leaderboard reads.

    docker compose run --rm bench --help
    docker compose run --rm bench --label demo --duration 60 --concurrency 32

What it reports, and why each number is there:

  per-endpoint latency percentiles   API overhead, measured on its own. Submitting does not
                                     judge, so these numbers never include program execution.
  throughput and error counts        what the API sustained, and whether it stayed correct.
  queue delay                        time from a job being committed to it being leased, read
                                     from the stored records, not from the client side.
  execution time                     what the sandbox spent, so API overhead and program
                                     execution can be read apart rather than blended.

Every run is labelled with the commit, the configured execution mode, the dataset size and
shape, the concurrency, the request mix and the machine it ran on, and is written to
evidence/workload-<label>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import platform
import random
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from sqlalchemy import create_engine, text

from . import dataset as dataset_builder

API = os.environ.get("API_BASE_URL", "http://api:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://shodh:shodh_dev_password@postgres:5432/shodh")
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))
PROGRAMS = pathlib.Path("/srv/seed/data/programs")


@dataclass
class Sample:
    endpoint: str
    status: int
    duration_ms: float


@dataclass
class Recorder:
    samples: list[Sample] = field(default_factory=list)

    def add(self, endpoint: str, status: int, duration_ms: float) -> None:
        self.samples.append(Sample(endpoint, status, duration_ms))

    def by_endpoint(self) -> dict[str, list[Sample]]:
        grouped: dict[str, list[Sample]] = defaultdict(list)
        for sample in self.samples:
            grouped[sample.endpoint].append(sample)
        return grouped


def percentiles(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return round(ordered[index], 2)

    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 2),
        "p50_ms": at(0.50),
        "p90_ms": at(0.90),
        "p95_ms": at(0.95),
        "p99_ms": at(0.99),
        "max_ms": round(ordered[-1], 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
    }


def machine() -> dict:
    info: dict = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count_visible": os.cpu_count(),
    }
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except OSError:
        pass
    for path, key in (
        ("/sys/fs/cgroup/memory.max", "cgroup_memory_max"),
        ("/sys/fs/cgroup/cpu.max", "cgroup_cpu_max"),
    ):
        try:
            info[key] = pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    info["note"] = (
        "Measured inside a container on this host; the API, the judge workers, PostgreSQL and "
        "this harness all share these resources, so absolute numbers are a property of the "
        "machine as much as of the code. Comparisons are only made between runs on the same host."
    )
    return info


async def virtual_user(
    index: int,
    *,
    data: dataset_builder.Dataset,
    recorder: Recorder,
    deadline: float,
    submit_share: float,
    seed: int,
) -> None:
    rng = random.Random(seed + index)
    participant = data.participants[index % len(data.participants)]
    source = (PROGRAMS / "sum_two_accepted.py").read_text(encoding="utf-8")
    headers = {"Authorization": f"Bearer {participant['token']}"}

    async with httpx.AsyncClient(base_url=API, headers=headers, timeout=30.0) as client:
        pending: list[str] = []
        while time.monotonic() < deadline:
            roll = rng.random()
            if roll < submit_share:
                problem = rng.choice(data.problems)
                key = f"wl-{uuid.uuid4().hex}"
                started = time.perf_counter()
                response = await client.post(
                    f"/contests/{data.contest_id}/submissions",
                    json={
                        "problem_id": problem["id"],
                        "language": "python3",
                        "source_code": f"{source}\n# {key}\n",
                    },
                    headers={"Idempotency-Key": key},
                )
                recorder.add("POST /submissions", response.status_code, (time.perf_counter() - started) * 1000)
                if response.status_code in (200, 201):
                    pending.append(response.json()["id"])
                    pending[:] = pending[-20:]
            elif roll < submit_share + 0.1 and pending:
                submission_id = rng.choice(pending)
                started = time.perf_counter()
                response = await client.get(f"/submissions/{submission_id}")
                recorder.add("GET /submissions/{id}", response.status_code, (time.perf_counter() - started) * 1000)
            elif roll < submit_share + 0.2:
                started = time.perf_counter()
                response = await client.get(f"/contests/{data.contest_id}/submissions?limit=20")
                recorder.add("GET /submissions", response.status_code, (time.perf_counter() - started) * 1000)
            else:
                offset = rng.choice([0, 0, 0, 20, 40])
                started = time.perf_counter()
                response = await client.get(
                    f"/contests/{data.contest_id}/leaderboard?limit=20&offset={offset}"
                )
                recorder.add("GET /leaderboard", response.status_code, (time.perf_counter() - started) * 1000)


def baseline(data: dataset_builder.Dataset, *, requests: int) -> dict:
    """Uncontended latency: one request at a time, nothing else offered to the API.

    Reported next to the concurrent run because a saturated percentile on its own says more
    about the queue in front of the service than about the work the service does.
    """
    participant = data.participants[0]
    source = (PROGRAMS / "sum_two_accepted.py").read_text(encoding="utf-8")
    out: dict[str, dict] = {}
    with dataset_builder.client(participant["token"]) as client:
        probes = {
            "GET /healthz": lambda: client.get("/healthz"),
            "GET /leaderboard": lambda: client.get(
                f"/contests/{data.contest_id}/leaderboard?limit=20"
            ),
            "GET /submissions": lambda: client.get(
                f"/contests/{data.contest_id}/submissions?limit=20"
            ),
        }
        for name, call in probes.items():
            timings = []
            for _ in range(requests):
                started = time.perf_counter()
                response = call()
                timings.append((time.perf_counter() - started) * 1000)
                response.raise_for_status()
            out[name] = percentiles(timings)

        timings = []
        for index in range(max(5, requests // 3)):
            key = f"baseline-{uuid.uuid4().hex}"
            started = time.perf_counter()
            response = client.post(
                f"/contests/{data.contest_id}/submissions",
                json={
                    "problem_id": data.problems[0]["id"],
                    "language": "python3",
                    "source_code": f"{source}\n# baseline {index}\n",
                },
                headers={"Idempotency-Key": key},
            )
            timings.append((time.perf_counter() - started) * 1000)
            response.raise_for_status()
        out["POST /submissions"] = percentiles(timings)
    return out


def queue_metrics(engine, contest_id: str, since: str) -> dict:
    """Queue delay and execution time, read from the stored records."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT a.id,
                       EXTRACT(EPOCH FROM (a.started_at - a.created_at)) * 1000 AS queue_delay_ms,
                       EXTRACT(EPOCH FROM (a.finished_at - a.started_at)) * 1000 AS judge_ms,
                       a.time_ms AS program_ms,
                       a.state,
                       a.exec_mode
                FROM judging_attempts a
                JOIN submissions s ON s.id = a.submission_id
                WHERE s.contest_id = :contest_id AND a.created_at >= CAST(:since AS timestamptz)
                """
            ),
            {"contest_id": contest_id, "since": since},
        ).mappings().all()
    queued = [float(r["queue_delay_ms"]) for r in rows if r["queue_delay_ms"] is not None]
    judged = [float(r["judge_ms"]) for r in rows if r["judge_ms"] is not None]
    program = [float(r["program_ms"]) for r in rows if r["program_ms"] is not None]
    states: dict[str, int] = defaultdict(int)
    modes: dict[str, int] = defaultdict(int)
    for row in rows:
        states[row["state"]] += 1
        modes[row["exec_mode"] or "not-judged-yet"] += 1
    return {
        "attempts_created_during_run": len(rows),
        "attempt_states": dict(states),
        "execution_modes": dict(modes),
        "queue_delay": percentiles(queued),
        "judge_wall_time": percentiles(judged),
        "program_execution_time": percentiles(program),
    }


def consistency_check(data: dataset_builder.Dataset) -> dict:
    with dataset_builder.client(data.organizer_token) as organizer:
        report = organizer.get(f"/contests/{data.contest_id}/standings/verify")
        report.raise_for_status()
        body = report.json()
    return {
        "serving_matches_rebuild": body["serving_matches_rebuild"],
        "serving_matches_naive": body["serving_matches_naive"],
        "rows": body["rows"],
    }


async def run(args) -> dict:
    engine = create_engine(DATABASE_URL, future=True)
    with dataset_builder.client() as probe:
        version = probe.get("/version").json()

    handle = pathlib.Path(args.dataset_file) if args.dataset_file else None
    if handle is None or not handle.exists():
        print(
            f"[workload] building dataset: {args.participants} participants x {args.problems} problems",
            flush=True,
        )
    data, reused = dataset_builder.load_or_build(
        handle,
        participants=args.participants,
        problems=args.problems,
        submissions_per_cell=args.prefill,
        seed=args.seed,
        label=args.label,
    )
    print(
        f"[workload] dataset {'reused' if reused else 'ready'}: {json.dumps(data.summary())}",
        flush=True,
    )
    if args.build_only:
        return {"built_only": True, "dataset": data.summary()}

    print(f"[workload] measuring uncontended baseline ({args.baseline} requests per endpoint)", flush=True)
    baseline_stats = baseline(data, requests=args.baseline) if args.baseline else {}

    recorder = Recorder()
    started_wall = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    since = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    started = time.monotonic()
    deadline = started + args.duration
    await asyncio.gather(
        *(
            virtual_user(
                index,
                data=data,
                recorder=recorder,
                deadline=deadline,
                submit_share=args.submit_share,
                seed=args.seed,
            )
            for index in range(args.concurrency)
        )
    )
    elapsed = time.monotonic() - started

    if args.drain:
        print(f"[workload] draining the queue for up to {args.drain}s", flush=True)
        drain_deadline = time.monotonic() + args.drain
        with dataset_builder.client(data.organizer_token) as organizer:
            while time.monotonic() < drain_deadline:
                queue = organizer.get("/ops/queue").json()["queue"]
                outstanding = sum(
                    queue.get(state, {}).get("count", 0) for state in ("READY", "LEASED")
                )
                if outstanding == 0:
                    break
                time.sleep(1.0)

    grouped = recorder.by_endpoint()
    per_endpoint = {}
    total_errors = 0
    for endpoint, samples in sorted(grouped.items()):
        errors = [s for s in samples if s.status >= 400]
        total_errors += len(errors)
        stats = percentiles([s.duration_ms for s in samples])
        stats["requests_per_second"] = round(len(samples) / elapsed, 2)
        stats["errors"] = len(errors)
        stats["status_codes"] = dict(sorted(defaultdict(int, _counts(samples)).items()))
        per_endpoint[endpoint] = stats

    report = {
        "label": args.label,
        "started_at": started_wall,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(elapsed, 2),
        "configuration": {
            "concurrency": args.concurrency,
            "submit_share": args.submit_share,
            "request_mix": {
                "POST /submissions": args.submit_share,
                "GET /submissions/{id}": 0.1,
                "GET /submissions": 0.1,
                "GET /leaderboard": round(1 - args.submit_share - 0.2, 3),
            },
            "seed": args.seed,
            "drain_seconds": args.drain,
        },
        "environment": {
            "commit": version.get("commit"),
            "execution_mode": version.get("execution_mode"),
            "lease_seconds": version.get("lease_seconds"),
            "max_deliveries": version.get("max_deliveries"),
            "api_base_url": API,
            "api_workers_env": os.environ.get("API_WORKERS", "unset"),
            "worker_replicas_env": os.environ.get("WORKER_REPLICAS", "unset"),
            "machine": machine(),
        },
        "dataset": data.summary(),
        "baseline_sequential": baseline_stats,
        "results": {
            "total_requests": len(recorder.samples),
            "throughput_rps": round(len(recorder.samples) / elapsed, 2),
            "errors": total_errors,
            "error_rate": round(total_errors / max(1, len(recorder.samples)), 5),
            "per_endpoint": per_endpoint,
        },
        "judging": queue_metrics(engine, data.contest_id, since),
        "consistency_after_run": consistency_check(data),
    }
    engine.dispose()
    return report


def _counts(samples: list[Sample]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for sample in samples:
        counts[sample.status] += 1
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="default", help="name this run; used in the evidence filename")
    parser.add_argument("--duration", type=float, default=30.0, help="measured seconds")
    parser.add_argument("--concurrency", type=int, default=16, help="virtual users")
    parser.add_argument("--participants", type=int, default=60)
    parser.add_argument("--problems", type=int, default=4)
    parser.add_argument("--prefill", type=int, default=3, help="judged submissions per (participant, problem)")
    parser.add_argument("--submit-share", type=float, default=0.3, dest="submit_share")
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--drain", type=float, default=0.0, help="seconds to wait for the queue to empty")
    parser.add_argument(
        "--baseline", type=int, default=30, help="uncontended requests per endpoint before the concurrent run"
    )
    parser.add_argument(
        "--dataset-file",
        dest="dataset_file",
        default="",
        help="reuse (or create) a dataset handle, so a run is repeatable against identical data",
    )
    parser.add_argument(
        "--build-only",
        dest="build_only",
        action="store_true",
        help="build the dataset and stop; lets the dataset be prepared with the judge stopped",
    )
    args = parser.parse_args()

    report = asyncio.run(run(args))
    if report.get("built_only"):
        print(f"[workload] dataset built: {json.dumps(report['dataset'])}")
        return 0
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"workload-{args.label}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print("=" * 78)
    print(f"workload {args.label}: {report['results']['total_requests']} requests in {report['duration_s']}s")
    print(f"  commit={report['environment']['commit']} execution_mode={report['environment']['execution_mode']}")
    print(f"  dataset={json.dumps(report['dataset'])}")
    print(f"  throughput={report['results']['throughput_rps']} rps  errors={report['results']['errors']}")
    print("-" * 78)
    print(f"{'endpoint':28s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s} {'rps':>7s} {'err':>5s}")
    for endpoint, stats in report["results"]["per_endpoint"].items():
        print(
            f"{endpoint:28s} {stats['count']:6d} {stats['p50_ms']:8.2f} {stats['p95_ms']:8.2f} "
            f"{stats['p99_ms']:8.2f} {stats['max_ms']:8.2f} {stats['requests_per_second']:7.1f} "
            f"{stats['errors']:5d}"
        )
    if report["baseline_sequential"]:
        print("-" * 78)
        print("uncontended baseline (one request at a time)")
        for endpoint, stats in report["baseline_sequential"].items():
            print(
                f"{endpoint:28s} {stats['count']:6d} {stats['p50_ms']:8.2f} {stats['p95_ms']:8.2f} "
                f"{stats['max_ms']:8.2f}"
            )
    print("-" * 78)
    judging = report["judging"]
    print(f"attempts created during the run: {judging['attempts_created_during_run']}")
    print(f"  states           {judging['attempt_states']}")
    print(f"  execution modes  {judging['execution_modes']}")
    if judging["queue_delay"]:
        print(
            f"  queue delay      p50={judging['queue_delay']['p50_ms']}ms "
            f"p95={judging['queue_delay']['p95_ms']}ms"
        )
    if judging["program_execution_time"]:
        print(
            f"  program time     p50={judging['program_execution_time']['p50_ms']}ms "
            f"p95={judging['program_execution_time']['p95_ms']}ms"
        )
    print(f"consistency after the run: {report['consistency_after_run']}")
    print(f"[evidence] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
