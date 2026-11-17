"""Judge worker.

Loop: claim leases, judge, report. Several of these run at once (two by default) and nothing
in here coordinates with the others - all mutual exclusion is in the database, behind the
lease protocol. A worker is disposable: kill it mid-judge and its work is reclaimed on lease
expiry, with its old delivery fenced out by the lease epoch.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/srv/api")  # reuse the API structured-logging setup

from app.logging_setup import bind, clear, setup_logging  # noqa: E402

from .client import JudgeApiUnavailable, JudgeClient, StaleLease  # noqa: E402
from .compare import classify  # noqa: E402
from .runners import RunnerUnavailable, build_runner  # noqa: E402

log = logging.getLogger("shodh.worker")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000")
JUDGE_TOKEN = os.environ.get("JUDGE_SERVICE_TOKEN", "shodh_judge_dev_token")
WORKER_ID = os.environ.get("WORKER_ID") or f"worker-{socket.gethostname()}-{os.getpid()}"
CONCURRENCY = int(os.environ.get("JUDGE_CONCURRENCY", "2"))
EXEC_MODE = os.environ.get("EXEC_MODE", "docker")
POLL_INTERVAL = float(os.environ.get("JUDGE_POLL_SECONDS", "0.25"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "30"))

# Worker-side failpoints, armed by the environment only.
WORKER_FAILPOINTS = {f.strip() for f in os.environ.get("WORKER_FAILPOINTS", "").split(",") if f.strip()}
CRASH_BEFORE_RESULT_ACK = "crash_before_result_ack"
RUNNER_UNAVAILABLE = "runner_unavailable"

# A submitted program may carry "SHODH_FAILPOINT:<name>" to make the judge fail on purpose.
# Only these two are reachable that way, and only while the fixture is enabled: a participant
# must never be able to kill a worker or skip an acknowledgement.
SOURCE_MARKER = "SHODH_FAILPOINT:"
SOURCE_ALLOWED = {"runner_unavailable", "runner_crash_in_container"}
ALLOW_SOURCE_FAILPOINTS = os.environ.get("ALLOW_SOURCE_FAILPOINTS", "true").lower() == "true"

_stop = threading.Event()


def source_failpoint(source_code: str) -> str | None:
    if not ALLOW_SOURCE_FAILPOINTS:
        return None
    for line in source_code.splitlines()[:20]:
        index = line.find(SOURCE_MARKER)
        if index >= 0:
            name = line[index + len(SOURCE_MARKER) :].strip().strip("*/#").strip()
            if name in SOURCE_ALLOWED:
                return name
    return None


class Heartbeat:
    """Keeps a lease alive while a judge run is in progress."""

    def __init__(self, client: JudgeClient, attempt_id: str, lease_epoch: int):
        self._client = client
        self._attempt_id = attempt_id
        self._epoch = lease_epoch
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"hb-{attempt_id[:8]}")

    def _run(self) -> None:
        interval = max(1.0, LEASE_SECONDS / 3.0)
        while not self._stop.wait(interval):
            try:
                self._client.heartbeat(self._attempt_id, self._epoch)
            except StaleLease:
                log.warning("lease lost while judging", extra={"attempt_id": self._attempt_id})
                return
            except Exception as exc:  # pragma: no cover
                log.warning("heartbeat failed", extra={"attempt_id": self._attempt_id, "error": str(exc)})

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


def judge_one(client: JudgeClient, runner, lease: dict) -> None:
    payload = lease["payload"]
    attempt_id = str(lease["attempt_id"])
    lease_epoch = int(lease["lease_epoch"])
    request_id = payload.get("request_id")
    clear()
    bind(
        request_id=request_id,
        attempt_id=attempt_id,
        submission_id=payload.get("submission_id"),
        worker_id=WORKER_ID,
        lease_epoch=lease_epoch,
        exec_mode=runner.mode,
    )
    log.info("judging started", extra={"attempt_no": payload.get("attempt_no"), "delivery": lease.get("delivery_count")})

    marker = source_failpoint(payload["source_code"])
    if marker == "runner_crash_in_container":
        payload["sandbox_failpoint"] = "runner_crash_in_container"

    try:
        with Heartbeat(client, attempt_id, lease_epoch):
            if RUNNER_UNAVAILABLE in WORKER_FAILPOINTS or marker == "runner_unavailable":
                raise RunnerUnavailable("runner unavailable (failpoint)")
            outcome = runner.run(payload)
    except RunnerUnavailable as exc:
        log.warning("judge infrastructure failure", extra={"reason": str(exc)[:300]})
        try:
            client.report_infra_failure(attempt_id, lease_epoch, str(exc), request_id=request_id)
        except StaleLease:
            log.warning("infra failure report was stale; another delivery owns this attempt")
        except JudgeApiUnavailable as api_exc:
            log.error("could not report infra failure", extra={"error": str(api_exc)[:300]})
        return

    config = payload["runtime_config"]
    expected = {int(case["ordinal"]): case["expected_output"] for case in payload["test_cases"]}
    case_ids = {int(case["ordinal"]): case["test_case_id"] for case in payload["test_cases"]}
    tests = []
    for observation in outcome.observations:
        status = classify(
            observation,
            expected_output=expected.get(observation.ordinal, ""),
            compare_mode=config["compare_mode"],
            time_limit_ms=int(config["time_limit_ms"]),
            memory_limit_mb=int(config["memory_limit_mb"]),
        )
        tests.append(
            {
                "test_case_id": case_ids[observation.ordinal],
                "status": status,
                "time_ms": observation.time_ms,
                "memory_kb": observation.memory_kb,
                "exit_code": observation.exit_code,
                "output_bytes": observation.output_bytes,
                "truncated": observation.truncated,
            }
        )

    result = {
        "lease_epoch": lease_epoch,
        "worker_id": WORKER_ID,
        "exec_mode": outcome.exec_mode,
        "runner_image": outcome.runner_image,
        "compile": {"ok": outcome.compile_ok, "message": outcome.compile_message},
        "tests": tests,
        "time_ms": max((t["time_ms"] or 0) for t in tests) if tests else outcome.wall_ms,
        "memory_kb": max((t["memory_kb"] or 0) for t in tests) if tests else None,
    }

    if CRASH_BEFORE_RESULT_ACK in WORKER_FAILPOINTS:
        # Judged, then died before acknowledging. The lease expires, the work is reclaimed, and
        # the final stored outcome must still be exactly one verdict.
        log.error("exiting before acknowledging the result (failpoint)")
        os._exit(99)

    try:
        client.report_result(attempt_id, result, request_id=request_id)
        log.info(
            "result acknowledged",
            extra={
                "tests_reported": len(tests),
                "compile_ok": outcome.compile_ok,
                "judge_wall_ms": outcome.wall_ms,
            },
        )
    except StaleLease as exc:
        # Correct outcome, not an error: a newer delivery or a rejudge owns this attempt now.
        log.warning("result rejected as stale", extra={"detail": str(exc)[:300]})
    except JudgeApiUnavailable as exc:
        log.error("could not report result", extra={"error": str(exc)[:300]})


def main() -> int:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), f"worker:{WORKER_ID}")
    log.info(
        "worker starting",
        extra={"worker_id": WORKER_ID, "exec_mode": EXEC_MODE, "concurrency": CONCURRENCY},
    )

    def handle_signal(signum, _frame):  # pragma: no cover
        log.warning("shutdown signal received", extra={"signal": signum})
        _stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    client = JudgeClient(API_BASE_URL, JUDGE_TOKEN, worker_id=WORKER_ID)
    client.wait_until_ready()

    try:
        runner = build_runner(EXEC_MODE)
    except RunnerUnavailable as exc:
        log.error("no usable runner", extra={"error": str(exc)})
        return 2
    if hasattr(runner, "reap_strays"):
        runner.reap_strays()
    if hasattr(runner, "ensure_image"):
        for language, dockerfile in (("python3", "python/Dockerfile"), ("cpp17", "cpp/Dockerfile")):
            image = os.environ.get(
                f"RUNNER_IMAGE_{language.upper()}",
                "shodh-runner-python:1" if language == "python3" else "shodh-runner-cpp:1",
            )
            try:
                runner.ensure_image(language, image)
            except Exception as exc:
                log.error("runner image unavailable", extra={"language": language, "error": str(exc)[:300]})

    in_flight: set = set()
    lock = threading.Lock()

    def done(future, attempt_id: str) -> None:
        with lock:
            in_flight.discard(attempt_id)
        exc = future.exception()
        if exc is not None:  # pragma: no cover
            log.exception("judging task crashed", exc_info=exc)

    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="judge") as pool:
        while not _stop.is_set():
            with lock:
                free = CONCURRENCY - len(in_flight)
            if free <= 0:
                time.sleep(POLL_INTERVAL)
                continue
            try:
                leases = client.lease(free)
            except Exception as exc:
                log.warning("lease request failed", extra={"error": str(exc)[:300]})
                time.sleep(1.0)
                continue
            if not leases:
                time.sleep(POLL_INTERVAL)
                continue
            for lease in leases:
                attempt_id = str(lease["attempt_id"])
                with lock:
                    in_flight.add(attempt_id)
                future = pool.submit(judge_one, client, runner, lease)
                future.add_done_callback(lambda f, a=attempt_id: done(f, a))
        log.info("draining in-flight work before exit", extra={"in_flight": len(in_flight)})

    client.close()
    clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
