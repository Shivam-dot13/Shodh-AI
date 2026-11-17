"""Submission creation (idempotent), dispatch (transactional outbox) and rejudge."""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Principal
from ..config import get_settings
from ..domain import failpoints
from ..domain.enums import (
    AttemptKind,
    AttemptState,
    JobState,
    Language,
    SubmissionStatus,
)
from ..domain.errors import (
    AttemptLimitReached,
    ContestClosed,
    ContestNotStarted,
    IdempotencyConflict,
    NotAParticipant,
    NotFoundError,
    ValidationError,
)
from ..models import (
    AuditLog,
    Contest,
    ContestParticipant,
    JudgeJob,
    JudgingAttempt,
    Problem,
    ProblemVersion,
    Submission,
    TestSuite,
)
from ..runners import runner_image_for

log = logging.getLogger(__name__)
settings = get_settings()

MAX_SOURCE_BYTES = 256 * 1024


@dataclass
class SubmissionResult:
    submission: Submission
    created: bool
    attempt: JudgingAttempt | None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(problem_id: uuid.UUID, language: Language, source_code: str) -> str:
    """Semantic identity of a submission request.

    Reusing an idempotency key with a different fingerprint is a conflict, not a replay.
    """
    return sha256_text(f"{problem_id}|{language.value}|{sha256_text(source_code)}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_window_open(contest: Contest) -> None:
    now = _now()
    if now < contest.starts_at:
        raise ContestNotStarted(
            f"contest opens at {contest.starts_at.isoformat()}",
            details={"starts_at": contest.starts_at.isoformat()},
        )
    if now >= contest.ends_at:
        raise ContestClosed(
            f"contest closed at {contest.ends_at.isoformat()}",
            details={"ends_at": contest.ends_at.isoformat()},
        )


def _assert_participant(session: Session, contest: Contest, user_id: uuid.UUID) -> None:
    joined = session.execute(
        select(ContestParticipant.id).where(
            ContestParticipant.contest_id == contest.id, ContestParticipant.user_id == user_id
        )
    ).scalar_one_or_none()
    if joined is None:
        raise NotAParticipant("join the contest before submitting")


def runtime_config_for(version: ProblemVersion, language: Language) -> dict:
    """Frozen execution contract for one attempt.

    Stored on the attempt so a reviewer can see exactly which limits and which runner image
    produced a historical verdict, even after the problem or the deployment changes.
    """
    return {
        "language": language.value,
        "time_limit_ms": version.time_limit_ms,
        "memory_limit_mb": version.memory_limit_mb,
        "output_limit_bytes": version.output_limit_bytes,
        "compare_mode": version.compare_mode.value,
        "runner_image": runner_image_for(language),
        "pids_limit": 64,
        "cpus": 1.0,
        "exec_mode_requested": settings.exec_mode,
    }


def _new_attempt(
    session: Session,
    submission: Submission,
    version: ProblemVersion,
    suite: TestSuite,
    *,
    attempt_no: int,
    kind: AttemptKind,
    created_by: uuid.UUID | None,
    request_id: str | None,
) -> JudgingAttempt:
    attempt = JudgingAttempt(
        id=uuid.uuid4(),
        submission_id=submission.id,
        attempt_no=attempt_no,
        kind=kind,
        problem_version_id=version.id,
        test_suite_id=suite.id,
        runtime_config=runtime_config_for(version, submission.language),
        state=AttemptState.PENDING,
        max_score=version.max_points,
        created_by=created_by,
        request_id=request_id,
    )
    session.add(attempt)
    # Transactional outbox: the queue row is written in the same transaction as the attempt, so
    # a crash between "submission committed" and "job dispatched" is not representable.
    session.add(
        JudgeJob(
            id=uuid.uuid4(),
            attempt_id=attempt.id,
            max_deliveries=settings.max_deliveries,
            priority=100 if kind is AttemptKind.INITIAL else 200,
        )
    )
    return attempt


def _resolve_problem(session: Session, contest: Contest, problem_id: uuid.UUID) -> tuple[Problem, ProblemVersion, TestSuite]:
    problem = session.get(Problem, problem_id)
    if problem is None or problem.contest_id != contest.id:
        raise NotFoundError("problem not found")
    if problem.current_version_id is None:
        raise ValidationError("problem has no published version")
    version = session.get(ProblemVersion, problem.current_version_id)
    if version is None or version.default_test_suite_id is None:
        raise ValidationError("problem version has no test suite")
    suite = session.get(TestSuite, version.default_test_suite_id)
    if suite is None:
        raise ValidationError("problem version has no test suite")
    return problem, version, suite


def create_submission(
    session: Session,
    principal: Principal,
    contest: Contest,
    *,
    problem_id: uuid.UUID,
    language: Language,
    source_code: str,
    idempotency_key: str,
    request_id: str | None = None,
) -> SubmissionResult:
    """Create a submission, or return the one this key already created.

    Replay rules:
      same key + same fingerprint from the same actor in the same contest -> the same submission
      same key + different fingerprint                                     -> 409 conflict
    The replay path runs after authentication and tenancy checks but deliberately skips the
    contest-window check: a retry of a submission that was accepted while the contest was open
    must still return that submission after it closes.
    """
    if not source_code.strip():
        raise ValidationError("source_code must not be empty")
    if len(source_code.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValidationError(f"source_code exceeds {MAX_SOURCE_BYTES} bytes")
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValidationError("idempotency_key must be 1..200 characters")

    assert principal.user_id is not None
    fp = fingerprint(problem_id, language, source_code)

    existing = session.execute(
        select(Submission).where(
            Submission.contest_id == contest.id,
            Submission.user_id == principal.user_id,
            Submission.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _replay(existing, fp)

    _assert_participant(session, contest, principal.user_id)
    _assert_window_open(contest)
    problem, version, suite = _resolve_problem(session, contest, problem_id)

    if contest.max_attempts_per_problem is not None:
        used = session.execute(
            select(func.count(Submission.id)).where(
                Submission.contest_id == contest.id,
                Submission.user_id == principal.user_id,
                Submission.problem_id == problem_id,
            )
        ).scalar_one()
        if used >= contest.max_attempts_per_problem:
            raise AttemptLimitReached(
                f"at most {contest.max_attempts_per_problem} submissions per problem",
                details={"limit": contest.max_attempts_per_problem, "used": used},
            )

    submission = Submission(
        id=uuid.uuid4(),
        contest_id=contest.id,
        problem_id=problem.id,
        user_id=principal.user_id,
        language=language,
        source_code=source_code,
        source_sha256=sha256_text(source_code),
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
        status=SubmissionStatus.QUEUED,
        request_id=request_id,
    )
    session.add(submission)
    attempt = _new_attempt(
        session,
        submission,
        version,
        suite,
        attempt_no=1,
        kind=AttemptKind.INITIAL,
        created_by=principal.user_id,
        request_id=request_id,
    )
    try:
        session.commit()
    except IntegrityError:
        # Lost the race on the idempotency key: another request committed first.
        session.rollback()
        winner = session.execute(
            select(Submission).where(
                Submission.contest_id == contest.id,
                Submission.user_id == principal.user_id,
                Submission.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        log.info("idempotency race resolved", extra={"submission_id": str(winner.id)})
        return _replay(winner, fp)

    log.info(
        "submission accepted",
        extra={
            "submission_id": str(submission.id),
            "attempt_id": str(attempt.id),
            "contest_id": str(contest.id),
            "problem_id": str(problem.id),
            "language": language.value,
        },
    )
    # Armed only by tests: proves a crash after commit still leaves a dispatchable job.
    failpoints.trip(
        failpoints.CRASH_AFTER_SUBMISSION_COMMIT,
        "injected failure after submission commit, before response",
    )
    return SubmissionResult(submission=submission, created=True, attempt=attempt)


def _replay(existing: Submission, fp: str) -> SubmissionResult:
    if existing.request_fingerprint != fp:
        raise IdempotencyConflict(
            "idempotency key already used with a different submission",
            details={"submission_id": str(existing.id)},
        )
    return SubmissionResult(submission=existing, created=False, attempt=None)


def rejudge(
    session: Session,
    principal: Principal,
    submission: Submission,
    *,
    problem_version_id: uuid.UUID,
    test_suite_id: uuid.UUID,
    request_id: str | None = None,
) -> JudgingAttempt:
    """Queue a new judging attempt against an explicitly chosen version pair.

    Earlier attempts are preserved. The new attempt gets the next attempt_no, which makes it
    the one that will be authoritative once it completes; any in-flight older attempt is
    stopped from being redelivered, and if it reports anyway its result is stored as history
    without moving the standings.
    """
    version = session.get(ProblemVersion, problem_version_id)
    suite = session.get(TestSuite, test_suite_id)
    if version is None or version.problem_id != submission.problem_id:
        raise ValidationError("problem_version_id does not belong to this problem")
    if suite is None or suite.problem_id != submission.problem_id:
        raise ValidationError("test_suite_id does not belong to this problem")

    locked = session.execute(
        select(Submission).where(Submission.id == submission.id).with_for_update()
    ).scalar_one()
    last_no = session.execute(
        select(func.max(JudgingAttempt.attempt_no)).where(JudgingAttempt.submission_id == locked.id)
    ).scalar_one() or 0

    # Stop redelivery of superseded work; the attempt rows themselves are kept.
    open_jobs = session.execute(
        select(JudgeJob)
        .join(JudgingAttempt, JudgingAttempt.id == JudgeJob.attempt_id)
        .where(
            JudgingAttempt.submission_id == locked.id,
            JudgingAttempt.state.in_([AttemptState.PENDING, AttemptState.DISPATCHED, AttemptState.RUNNING]),
        )
        .with_for_update(of=JudgeJob)
    ).scalars().all()
    for job in open_jobs:
        job.state = JobState.DONE
        job.last_error = "superseded by rejudge"

    attempt = _new_attempt(
        session,
        locked,
        version,
        suite,
        attempt_no=last_no + 1,
        kind=AttemptKind.REJUDGE,
        created_by=principal.user_id,
        request_id=request_id,
    )
    locked.status = SubmissionStatus.QUEUED
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            actor_user_id=principal.user_id,
            actor_label=principal.label,
            action="rejudge",
            target_type="submission",
            target_id=str(locked.id),
            request_id=request_id,
            detail={
                "attempt_no": attempt.attempt_no,
                "problem_version_id": str(problem_version_id),
                "test_suite_id": str(test_suite_id),
                "superseded_jobs": [str(j.id) for j in open_jobs],
            },
        )
    )
    session.commit()
    log.info(
        "rejudge queued",
        extra={
            "submission_id": str(locked.id),
            "attempt_id": str(attempt.id),
            "attempt_no": attempt.attempt_no,
        },
    )
    return attempt


def refresh_submission_status(session: Session, submission: Submission) -> Submission:
    """Derive submission.status and authoritative_attempt_id from its attempts.

    status                 reflects whether a verdict exists at all
    authoritative_attempt  is the COMPLETED attempt with the highest attempt_no
    An attempt that failed for infrastructure reasons never becomes authoritative, so a runner
    crash leaves an earlier verdict - and the score it earned - in place.
    """
    attempts = session.execute(
        select(JudgingAttempt)
        .where(JudgingAttempt.submission_id == submission.id)
        .order_by(JudgingAttempt.attempt_no.desc())
    ).scalars().all()
    authoritative = next((a for a in attempts if a.state is AttemptState.COMPLETED), None)
    latest = attempts[0] if attempts else None

    submission.authoritative_attempt_id = authoritative.id if authoritative else None
    if authoritative is not None and (latest is None or latest.attempt_no == authoritative.attempt_no):
        submission.status = SubmissionStatus.JUDGED
    elif latest is None:
        submission.status = SubmissionStatus.QUEUED
    elif latest.state in (AttemptState.PENDING, AttemptState.DISPATCHED):
        submission.status = SubmissionStatus.QUEUED
    elif latest.state is AttemptState.RUNNING:
        submission.status = SubmissionStatus.RUNNING
    elif latest.state is AttemptState.INFRA_FAILED:
        submission.status = (
            SubmissionStatus.JUDGED if authoritative is not None else SubmissionStatus.JUDGE_FAILED
        )
    else:  # CANCELLED
        submission.status = (
            SubmissionStatus.JUDGED if authoritative is not None else SubmissionStatus.JUDGE_FAILED
        )
    session.flush()
    return submission
