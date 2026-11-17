"""The measured bottleneck, before and after, on the same workload.

    docker compose run --rm --entrypoint python bench -m bench.bottleneck --participants 400

The leaderboard is the hottest read in a contest platform: every participant polls it. Two
implementations of exactly the same answer are kept in the code so the difference can be
measured rather than asserted:

  naive      derive every standings cell from submissions and judging_attempts per request
             (DISTINCT ON to pick each submission authoritative attempt, then window functions
             for the ICPC rules)
  standings  read the problem_scores cells that the verdict transaction already maintained

This script builds one dataset, measures both paths against it, captures a query plan for each,
and then checks that they still produce the same standings - a faster leaderboard that
disagrees with the records would be worthless.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import time

from . import dataset as dataset_builder
from .workload import machine, percentiles

EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))


def measure(client, contest_id: str, source: str, *, requests: int, limit: int) -> dict:
    timings: list[float] = []
    compute: list[float] = []
    statuses: dict[int, int] = {}
    for _ in range(requests):
        started = time.perf_counter()
        response = client.get(
            f"/contests/{contest_id}/leaderboard?source={source}&limit={limit}&offset=0"
        )
        timings.append((time.perf_counter() - started) * 1000)
        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
        header = response.headers.get("X-Compute-Ms")
        if header:
            compute.append(float(header))
    stats = percentiles(timings)
    stats["status_codes"] = statuses
    stats["server_compute_ms"] = percentiles(compute)
    return stats


def plan(client, contest_id: str, source: str, limit: int) -> list[str]:
    response = client.get(f"/contests/{contest_id}/standings/explain?source={source}&limit={limit}")
    response.raise_for_status()
    return response.json()["plan"]


def plan_summary(lines: list[str]) -> dict:
    summary: dict = {"nodes": [], "execution_time_ms": None, "planning_time_ms": None, "shared_read": None}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Execution Time:"):
            summary["execution_time_ms"] = float(stripped.split(":")[1].strip().split(" ")[0])
        elif stripped.startswith("Planning Time:"):
            summary["planning_time_ms"] = float(stripped.split(":")[1].strip().split(" ")[0])
        elif "shared hit=" in stripped:
            summary["shared_read"] = stripped
        elif stripped.startswith("->") or "cost=" in stripped:
            name = stripped.lstrip("-> ").split("  (")[0]
            if name and len(summary["nodes"]) < 14:
                summary["nodes"].append(name)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--participants", type=int, default=300)
    parser.add_argument("--problems", type=int, default=6)
    parser.add_argument("--prefill", type=int, default=3)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--label", default="leaderboard")
    args = parser.parse_args()

    with dataset_builder.client() as probe:
        version = probe.get("/version").json()

    print(
        f"[bottleneck] building dataset: {args.participants} participants x {args.problems} problems "
        f"x {args.prefill} judged submissions",
        flush=True,
    )
    handle = pathlib.Path(f"/srv/evidence/.dataset-bottleneck-{args.label}.json")
    if handle.exists():
        data = dataset_builder.Dataset.load(handle)
        print(f"[bottleneck] reusing dataset {handle}", flush=True)
    else:
        data = dataset_builder.build(
            participants=args.participants,
            problems=args.problems,
            submissions_per_cell=args.prefill,
            label=f"bottleneck-{args.label}",
        )
        data.save(handle)
    print(f"[bottleneck] dataset ready: {json.dumps(data.summary())}", flush=True)

    organizer = dataset_builder.client(data.organizer_token, timeout=180.0)
    try:
        # Warm both paths so the comparison is not a cold-cache artefact.
        measure(organizer, data.contest_id, "naive", requests=3, limit=args.limit)
        measure(organizer, data.contest_id, "standings", requests=3, limit=args.limit)

        before = measure(organizer, data.contest_id, "naive", requests=args.requests, limit=args.limit)
        after = measure(organizer, data.contest_id, "standings", requests=args.requests, limit=args.limit)
        before_plan = plan(organizer, data.contest_id, "naive", args.limit)
        after_plan = plan(organizer, data.contest_id, "standings", args.limit)
        verify = organizer.get(f"/contests/{data.contest_id}/standings/verify?include_rows=true").json()
        rebuilt = organizer.post(f"/contests/{data.contest_id}/standings/rebuild").json()
    finally:
        organizer.close()

    improvement = (
        round(before["p95_ms"] / after["p95_ms"], 2) if after.get("p95_ms") else None
    )
    report = {
        "label": args.label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": {
            "commit": version.get("commit"),
            "execution_mode": version.get("execution_mode"),
            "machine": machine(),
        },
        "dataset": data.summary(),
        "workload": {"requests_per_path": args.requests, "page_limit": args.limit},
        "before_naive_derivation": {"latency": before, "plan_summary": plan_summary(before_plan)},
        "after_maintained_cells": {"latency": after, "plan_summary": plan_summary(after_plan)},
        "improvement": {
            "p50_speedup": round(before["p50_ms"] / after["p50_ms"], 2) if after.get("p50_ms") else None,
            "p95_speedup": improvement,
            "p95_ms_before": before.get("p95_ms"),
            "p95_ms_after": after.get("p95_ms"),
        },
        "correctness_after_change": {
            "serving_matches_rebuild": verify["serving_matches_rebuild"],
            "serving_matches_naive": verify["serving_matches_naive"],
            "rows": verify["rows"],
            "rebuild_changed_anything": rebuilt["changed"],
            "cells_rebuilt": rebuilt["cells_written"],
        },
        "trade_off": (
            "The maintained cells cost one extra locked row update inside every verdict "
            "transaction, and the standings_version counter serialises verdict commits for the "
            "same contest. In exchange the read path touches one small indexed table instead of "
            "re-deriving the whole contest per request. Judging throughput is bounded by sandbox "
            "start-up (tens of milliseconds at best), so paying a row lock per verdict is a good "
            "trade for a read that every participant polls."
        ),
        "plans": {"naive": before_plan, "standings": after_plan},
    }

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"bottleneck-{args.label}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print("=" * 78)
    print(f"leaderboard bottleneck, dataset {json.dumps(data.summary())}")
    print("-" * 78)
    for name, block in (("before (naive)", before), ("after (maintained)", after)):
        print(
            f"{name:20s} p50={block['p50_ms']:8.2f}ms p95={block['p95_ms']:8.2f}ms "
            f"p99={block['p99_ms']:8.2f}ms max={block['max_ms']:8.2f}ms n={block['count']}"
        )
    print(f"p95 speedup: {improvement}x")
    print("-" * 78)
    print(f"naive plan:     {report['before_naive_derivation']['plan_summary']['nodes'][:4]}")
    print(f"  execution:    {report['before_naive_derivation']['plan_summary']['execution_time_ms']} ms")
    print(f"standings plan: {report['after_maintained_cells']['plan_summary']['nodes'][:4]}")
    print(f"  execution:    {report['after_maintained_cells']['plan_summary']['execution_time_ms']} ms")
    print("-" * 78)
    print(f"correctness after the change: {json.dumps(report['correctness_after_change'])}")
    print(f"[evidence] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
