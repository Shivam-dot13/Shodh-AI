"""Collects everything under evidence/ into one summary a reviewer can read first.

Names the commit, the configuration, the data version and the execution mode each result was
produced under, so no number in the report is unattributable.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import httpx

API = os.environ.get("API_BASE_URL", "http://api:8000")
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))


def read(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    version = httpx.get(f"{API}/version", timeout=15).json()
    seed = read(EVIDENCE / "seed-manifest.json") or {}

    suites = {}
    for path in sorted(EVIDENCE.glob("test-report-*.json")):
        report = read(path) or {}
        suites[path.stem.replace("test-report-", "")] = {
            "file": path.name,
            "totals": report.get("totals"),
            "labels": {
                key: report.get("labels", {}).get(key)
                for key in ("commit", "configured_execution_mode", "data_version", "workers_running", "markers_selected")
            },
            "failed_tests": [
                row["test"] for row in report.get("results", []) if row.get("outcome") == "failed"
            ],
        }

    workloads = {}
    for path in sorted(EVIDENCE.glob("workload-*.json")):
        report = read(path) or {}
        workloads[path.stem.replace("workload-", "")] = {
            "file": path.name,
            "duration_s": report.get("duration_s"),
            "concurrency": report.get("configuration", {}).get("concurrency"),
            "dataset": report.get("dataset"),
            "throughput_rps": report.get("results", {}).get("throughput_rps"),
            "errors": report.get("results", {}).get("errors"),
            "execution_mode": report.get("environment", {}).get("execution_mode"),
            "queue_delay_p95_ms": report.get("judging", {}).get("queue_delay", {}).get("p95_ms"),
            "consistency_after_run": report.get("consistency_after_run"),
            "per_endpoint_p95_ms": {
                endpoint: stats.get("p95_ms")
                for endpoint, stats in report.get("results", {}).get("per_endpoint", {}).items()
            },
        }

    bottlenecks = {}
    for path in sorted(EVIDENCE.glob("bottleneck-*.json")):
        report = read(path) or {}
        bottlenecks[path.stem.replace("bottleneck-", "")] = {
            "file": path.name,
            "dataset": report.get("dataset"),
            "improvement": report.get("improvement"),
            "correctness_after_change": report.get("correctness_after_change"),
        }

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": version.get("commit"),
        "execution_mode": version.get("execution_mode"),
        "configuration": {
            "lease_seconds": version.get("lease_seconds"),
            "max_deliveries": version.get("max_deliveries"),
            "languages": version.get("languages"),
            "api_workers_env": os.environ.get("API_WORKERS", "unset"),
            "worker_replicas_env": os.environ.get("WORKER_REPLICAS", "unset"),
        },
        "data_version": seed.get("data_version"),
        "programs_version": seed.get("programs_version"),
        "seeded_at": seed.get("seeded_at"),
        "test_suites": suites,
        "workloads": workloads,
        "bottleneck_investigations": bottlenecks,
        "demo_logs": sorted(path.name for path in EVIDENCE.glob("demo-*.log")),
        "failure_drills": sorted(path.name for path in EVIDENCE.glob("failure-drill-*.log")),
    }
    path = EVIDENCE / "evaluation-summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"[evidence] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
