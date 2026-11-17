"""The judge protocol: lease, heartbeat, result, infrastructure failure, reclaim.

Which guarantee comes from where
--------------------------------
Persistent storage (PostgreSQL)
    * One job row per attempt (unique attempt_id), so a retried dispatch cannot duplicate work.
    * SELECT ... FOR UPDATE SKIP LOCKED makes claiming atomic across any number of workers and
      any number of API processes; no in-process lock is involved anywhere.
    * The verdict write is a conditional UPDATE guarded by state and lease_epoch, so only one
      transaction can ever move an attempt into COMPLETED.
    * Scoring is a recompute of a row held FOR UPDATE, so points cannot be awarded twice.

Worker protocol
    * Leases expire. A worker must heartbeat to keep one; an abandoned lease is reclaimed.
    * lease_epoch is a fencing token: every lease and every reclaim bumps it, and a result is
      accepted only with the current value. A worker that was slow, paused or partitioned
      cannot overwrite the result of the delivery that replaced it.
    * Deliveries are bounded by max_deliveries; the last failure makes the attempt
      INFRA_FAILED, which carries no verdict and leaves any earlier verdict and score alone.
    * Delivery is at-least-once; the *effect* is exactly-once, because applying a result is
      idempotent under a fixed lease_epoch.

Runner (unavoidably)
    * Whether a program that exceeded a limit is reported as TLE/MLE rather than a crash, and
      whether the sandbox actually contained it. That is why the execution mode is recorded on
      every attempt and labelled in every test result.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.enums import AttemptState, JobState, TestStatus
from ..domain.errors import (
    IllegalStateTransition,
    NotFoundError,
    StaleJudgeResult,
    ValidationError,
)
from ..models import (
    AttemptTestResult,
    Contest,
    JudgeJob,
    JudgingAttempt,
    ProblemVersion,
    Submission,
    TestCase,
)
from .scores import recompute_cell
from .submissions import refresh_submission_status
from .verdicts import TestOutcome, decide

log = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class Lease:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    lease_epoch: int
    lease_expires_at: datetime
    delivery_count: int


_CLAIM_SQL = text(
    """
WITH candidate AS (
    SELECT j.id
    FROM judge_jobs j
    WHERE j.state = 'READY' AND j.available_at <= now()
    ORDER BY j.priority, j.available_at, j.id
    FOR UPDATE SKIP LOCKED
    LIMIT :capacity
)
UPDATE judge_jobs j
SET state = 'LEASED',
    leased_by = :worker_id,
    lease_expires_at = now() + make_interval(secs => :lease_seconds),
    delivery_count = j.delivery_count + 1,
    updated_at = now()
FROM candidate c
WHERE j.id = c.id
RETURNING j.id AS job_id, j.attempt_id, j.delivery_count, j.lease_expires_at
"""
)

# Bumping lease_epoch here is what fences out the previous holder of this attempt.
_TAKE_ATTEMPT_SQL = text(
    """
UPDATE judging_attempts a
SET state = 'DISPATCHED',
    lease_epoch = a.lease_epoch + 1,
    worker_id = :worker_id,
    started_at = COALESCE(a.started_at, now())
WHERE a.id = :attempt_id
  AND a.state IN ('PENDING', 'DISPATCHED', 'RUNNING')
RETURNING a.lease_epoch
"""
)


def lease_jobs(session: Session, *, worker_id: str, capacity: int = 1) -> list[Lease]:
    capacity = max(1, min(capacity, 16))
    claimed = session.execute(
        _CLAIM_SQL, {"capacity": capacity, "worker_id": worker_id, "lease_seconds": settings.lease_seconds}
    ).mappings().all()
    leases: list[Lease] = []
    for row in claimed:
        epoch = session.execute(
            _TAKE_ATTEMPT_SQL, {"attempt_id": row["attempt_id"], "worker_id": worker_id}
        ).scalar_one_or_none()
        if epoch is None:
            # The attempt was superseded or already finished: retire the job quietly.
            session.execute(
                text("UPDATE judge_jobs SET state = 'DONE', last_error = :e, updated_at = now() WHERE id = :id"),
                {"id": row["job_id"], "e": "attempt no longer leasable"},
            )
            continue
        leases.append(
            Lease(
                job_id=row["job_id"],
                attempt_id=row["attempt_id"],
                lease_epoch=epoch,
                lease_expires_at=row["lease_expires_at"],
                delivery_count=row["delivery_count"],
            )
        )
    session.commit()
    for lease in leases:
        log.info(
            "job leased",
            extra={
                "attempt_id": str(lease.attempt_id),
                "job_id": str(lease.job_id),
                "lease_epoch": lease.lease_epoch,
                "delivery_count": lease.delivery_count,
                "worker_id": worker_id,
            },
        )
    return leases


def _attempt_for_update(session: Session, attempt_id: uuid.UUID) -> JudgingAttempt:
    attempt = session.execute(
        select(JudgingAttempt).where(JudgingAttempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    if attempt is None:
        raise NotFoundError("attempt not found")
    return attempt


def _assert_epoch(attempt: JudgingAttempt, lease_epoch: int) -> None:
    if attempt.lease_epoch != lease_epoch:
        raise StaleJudgeResult(
            "lease epoch is stale; this delivery has been superseded",
            details={"expected": attempt.lease_epoch, "received": lease_epoch},
        )


def build_payload(session: Session, attempt_id: uuid.UUID) -> dict:
    """Everything the judge needs, and nothing a participant may ever see.

    Hidden inputs and expected outputs are released only on this route, which requires the
    judge service scope. The expected outputs stay in the worker: only inputs are copied into
    the sandbox, so submitted code never sees an expected answer even if it escaped.
    """
    attempt = session.get(JudgingAttempt, attempt_id)
    if attempt is None:
        raise NotFoundError("attempt not found")
    submission = session.get(Submission, attempt.submission_id)
    version = session.get(ProblemVersion, attempt.problem_version_id)
    assert submission is not None and version is not None
    cases = session.execute(
        select(TestCase).where(TestCase.test_suite_id == attempt.test_suite_id).order_by(TestCase.ordinal)
    ).scalars().all()
    return {
        "attempt_id": str(attempt.id),
        "attempt_no": attempt.attempt_no,
        "lease_epoch": attempt.lease_epoch,
        "submission_id": str(submission.id),
        "request_id": submission.request_id,
        "language": submission.language.value,
        "source_code": submission.source_code,
        "problem_version_id": str(version.id),
        "test_suite_id": str(attempt.test_suite_id),
        "runtime_config": attempt.runtime_config,
        "max_points": attempt.max_score,
        "test_cases": [
            {
                "test_case_id": str(case.id),
                "ordinal": case.ordinal,
                "weight": case.weight,
                "is_sample": case.is_sample,
                "input": case.input_data,
                "expected_output": case.expected_output,
            }
            for case in cases
        ],
    }


def heartbeat(session: Session, attempt_id: uuid.UUID, lease_epoch: int) -> datetime:
    attempt = _attempt_for_update(session, attempt_id)
    _assert_epoch(attempt, lease_epoch)
    if attempt.state not in (AttemptState.DISPATCHED, AttemptState.RUNNING):
        raise IllegalStateTransition(f"cannot heartbeat an attempt in state {attempt.state.value}")
    attempt.state = AttemptState.RUNNING
    expires = session.execute(
        text(
            """
            UPDATE judge_jobs
            SET lease_expires_at = now() + make_interval(secs => :lease_seconds), updated_at = now()
            WHERE attempt_id = :attempt_id AND state = 'LEASED'
            RETURNING lease_expires_at
            """
        ),
        {"attempt_id": attempt_id, "lease_seconds": settings.lease_seconds},
    ).scalar_one_or_none()
    if expires is None:
        session.rollback()
        raise StaleJudgeResult("lease is no longer held by this worker")
    session.commit()
    return expires


def apply_result(session: Session, attempt_id: uuid.UUID, payload: dict) -> JudgingAttempt:
    """Record a judging result and move the standings, exactly once.

    Returning the stored attempt unchanged when it is already COMPLETED at the same lease_epoch
    is what makes a lost acknowledgement safe: the worker can retry the ack after a crash and
    gets the same answer rather than a second score.
    """
    lease_epoch = int(payload["lease_epoch"])
    attempt = _attempt_for_update(session, attempt_id)

    if attempt.state is AttemptState.COMPLETED:
        if attempt.lease_epoch == lease_epoch:
            session.rollback()
            log.info("duplicate result ignored", extra={"attempt_id": str(attempt_id)})
            return attempt
        session.rollback()
        raise StaleJudgeResult("attempt already completed by another delivery")
    _assert_epoch(attempt, lease_epoch)
    if attempt.state not in (AttemptState.DISPATCHED, AttemptState.RUNNING):
        session.rollback()
        raise IllegalStateTransition(f"cannot complete an attempt in state {attempt.state.value}")

    cases = {
        str(case.id): case
        for case in session.execute(
            select(TestCase).where(TestCase.test_suite_id == attempt.test_suite_id)
        ).scalars()
    }
    reported = payload.get("tests") or []
    if not isinstance(reported, list):
        raise ValidationError("tests must be a list")
    seen: set[str] = set()
    outcomes: list[TestOutcome] = []
    rows: list[AttemptTestResult] = []
    for item in reported:
        case_id = str(item.get("test_case_id"))
        case = cases.get(case_id)
        if case is None:
            raise ValidationError(
                "result references a test case outside the pinned suite",
                details={"test_case_id": case_id},
            )
        if case_id in seen:
            raise ValidationError("duplicate test case in result", details={"test_case_id": case_id})
        seen.add(case_id)
        status = TestStatus(item["status"])
        outcomes.append(TestOutcome(ordinal=case.ordinal, status=status, weight=case.weight))
        rows.append(
            AttemptTestResult(
                id=uuid.uuid4(),
                attempt_id=attempt.id,
                test_case_id=case.id,
                ordinal=case.ordinal,
                is_sample=case.is_sample,
                status=status,
                time_ms=item.get("time_ms"),
                memory_kb=item.get("memory_kb"),
                exit_code=item.get("exit_code"),
                output_bytes=int(item.get("output_bytes") or 0),
                truncated=bool(item.get("truncated")),
            )
        )

    compiled = bool((payload.get("compile") or {}).get("ok", True))
    if compiled and len(seen) != len(cases):
        raise ValidationError(
            "result does not cover the pinned suite",
            details={"expected": len(cases), "received": len(seen)},
        )

    result = decide(compiled=compiled, outcomes=outcomes, max_points=attempt.max_score)

    for row in rows:
        session.add(row)
    attempt.state = AttemptState.COMPLETED
    attempt.verdict = result.verdict
    attempt.score = result.score
    attempt.tests_total = result.tests_total
    attempt.tests_passed = result.tests_passed
    attempt.time_ms = payload.get("time_ms")
    attempt.memory_kb = payload.get("memory_kb")
    attempt.exec_mode = payload.get("exec_mode")
    attempt.runner_image = payload.get("runner_image")
    attempt.worker_id = payload.get("worker_id") or attempt.worker_id
    attempt.compile_message = ((payload.get("compile") or {}).get("message") or None)
    # Any note left by an earlier, reclaimed delivery of this attempt is history that belongs
    # to the job row, not to a completed verdict.
    attempt.failure_reason = None
    attempt.finished_at = datetime.now().astimezone()
    session.execute(
        text("UPDATE judge_jobs SET state = 'DONE', updated_at = now() WHERE attempt_id = :a"),
        {"a": attempt.id},
    )
    session.flush()

    submission = session.get(Submission, attempt.submission_id)
    assert submission is not None
    refresh_submission_status(session, submission)
    contest = session.get(Contest, submission.contest_id)
    assert contest is not None
    recompute_cell(session, contest, submission.user_id, submission.problem_id)
    session.commit()

    log.info(
        "verdict stored",
        extra={
            "attempt_id": str(attempt.id),
            "submission_id": str(submission.id),
            "attempt_no": attempt.attempt_no,
            "verdict": result.verdict.value,
            "score": result.score,
            "tests_passed": result.tests_passed,
            "tests_total": result.tests_total,
            "exec_mode": attempt.exec_mode,
            "request_id": submission.request_id,
        },
    )
    return attempt


def report_infra_failure(
    session: Session, attempt_id: uuid.UUID, *, lease_epoch: int, reason: str, retryable: bool = True
) -> dict:
    """The judge could not judge. Never a student verdict, never a score change."""
    attempt = _attempt_for_update(session, attempt_id)
    if attempt.state is AttemptState.COMPLETED:
        session.rollback()
        raise IllegalStateTransition("attempt already completed")
    _assert_epoch(attempt, lease_epoch)

    job = session.execute(
        select(JudgeJob).where(JudgeJob.attempt_id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    exhausted = (not retryable) or job is None or job.delivery_count >= job.max_deliveries

    if exhausted:
        attempt.state = AttemptState.INFRA_FAILED
        attempt.verdict = None
        attempt.failure_reason = reason[:2000]
        attempt.finished_at = datetime.now().astimezone()
        if job is not None:
            job.state = JobState.DEAD
            job.last_error = reason[:2000]
    else:
        attempt.state = AttemptState.PENDING
        attempt.failure_reason = reason[:2000]
        job.state = JobState.READY
        job.last_error = reason[:2000]
        job.lease_expires_at = None
        job.leased_by = None
        session.execute(
            text(
                """
                UPDATE judge_jobs
                SET available_at = now() + make_interval(secs => LEAST(30, POWER(2, delivery_count)::int)),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": job.id},
        )
    session.flush()
    submission = session.get(Submission, attempt.submission_id)
    assert submission is not None
    refresh_submission_status(session, submission)
    session.commit()
    log.warning(
        "judge infrastructure failure",
        extra={
            "attempt_id": str(attempt_id),
            "submission_id": str(submission.id),
            "retryable": retryable and not exhausted,
            "delivery_count": job.delivery_count if job else None,
            "reason": reason[:200],
        },
    )
    return {
        "attempt_id": str(attempt_id),
        "state": attempt.state.value,
        "requeued": not exhausted,
        "delivery_count": job.delivery_count if job else None,
    }


_RECLAIM_RETRY_SQL = text(
    """
WITH expired AS (
    SELECT j.id, j.attempt_id
    FROM judge_jobs j
    WHERE j.state = 'LEASED'
      AND j.lease_expires_at < now()
      AND j.delivery_count < j.max_deliveries
    ORDER BY j.lease_expires_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch
), requeued AS (
    UPDATE judge_jobs j
    SET state = 'READY',
        leased_by = NULL,
        lease_expires_at = NULL,
        available_at = now(),
        last_error = 'lease expired; work reclaimed',
        updated_at = now()
    FROM expired e
    WHERE j.id = e.id
    RETURNING j.attempt_id
)
UPDATE judging_attempts a
SET state = 'PENDING',
    lease_epoch = a.lease_epoch + 1,
    worker_id = NULL,
    failure_reason = 'lease expired; work reclaimed'
FROM requeued r
WHERE a.id = r.attempt_id AND a.state IN ('DISPATCHED', 'RUNNING')
RETURNING a.id
"""
)

_RECLAIM_DEAD_SQL = text(
    """
WITH expired AS (
    SELECT j.id, j.attempt_id
    FROM judge_jobs j
    WHERE j.state = 'LEASED'
      AND j.lease_expires_at < now()
      AND j.delivery_count >= j.max_deliveries
    ORDER BY j.lease_expires_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch
), killed AS (
    UPDATE judge_jobs j
    SET state = 'DEAD',
        leased_by = NULL,
        lease_expires_at = NULL,
        last_error = 'lease expired; delivery budget exhausted',
        updated_at = now()
    FROM expired e
    WHERE j.id = e.id
    RETURNING j.attempt_id
)
UPDATE judging_attempts a
SET state = 'INFRA_FAILED',
    verdict = NULL,
    worker_id = NULL,
    finished_at = now(),
    failure_reason = 'lease expired; delivery budget exhausted'
FROM killed k
WHERE a.id = k.attempt_id AND a.state IN ('DISPATCHED', 'RUNNING')
RETURNING a.id, a.submission_id
"""
)


def reap_expired(session: Session, *, batch: int = 50) -> dict:
    """Recover abandoned work. Safe to run from every API process at once.

    Bumping lease_epoch on reclaim is deliberate: the worker that vanished is fenced out, so if
    it comes back to life mid-judge its result is rejected as stale instead of racing the new
    delivery.
    """
    requeued = session.execute(_RECLAIM_RETRY_SQL, {"batch": batch}).scalars().all()
    dead = session.execute(_RECLAIM_DEAD_SQL, {"batch": batch}).mappings().all()
    session.commit()
    for row in dead:
        submission = session.get(Submission, row["submission_id"])
        if submission is not None:
            refresh_submission_status(session, submission)
    if dead:
        session.commit()
    if requeued or dead:
        log.warning(
            "reaper recovered abandoned work",
            extra={"requeued": len(requeued), "dead": len(dead)},
        )
    return {"requeued": len(requeued), "dead": len(dead)}


def queue_stats(session: Session) -> dict:
    rows = session.execute(
        text(
            """
            SELECT state, COUNT(*) AS n,
                   COALESCE(MAX(EXTRACT(EPOCH FROM (now() - created_at))), 0) AS oldest_age_s
            FROM judge_jobs GROUP BY state
            """
        )
    ).mappings().all()
    return {row["state"]: {"count": int(row["n"]), "oldest_age_s": float(row["oldest_age_s"])} for row in rows}
