"""Participant submissions and organizer rejudges.

Accepting a submission does not judge it: the row and its first judging attempt are committed,
a queue row is written in the same transaction, and the identifier comes back immediately. All
progress is then observed through this resource.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Principal, require_organizer, require_participant, require_user
from ..config import get_settings
from ..db import get_session
from ..domain.enums import AttemptState, SubmissionStatus
from ..domain.errors import ValidationError
from ..logging_setup import current_request_id
from ..models import AttemptTestResult, JudgingAttempt, Submission
from ..schemas import (
    AttemptOrganizerOut,
    AttemptOut,
    BulkRejudgeOut,
    Page,
    PageEnvelope,
    RejudgeIn,
    SubmissionDetailOut,
    SubmissionIn,
    SubmissionOut,
    TestResultOut,
)
from ..services import access
from ..services import submissions as submission_service

router = APIRouter(tags=["submissions"])
settings = get_settings()


def _attempts_of(session: Session, submission_id: uuid.UUID) -> list[JudgingAttempt]:
    return list(
        session.execute(
            select(JudgingAttempt)
            .where(JudgingAttempt.submission_id == submission_id)
            .order_by(JudgingAttempt.attempt_no)
        ).scalars()
    )


def _submission_out(
    submission: Submission,
    attempts: list[JudgingAttempt],
    *,
    include_source: bool,
    replayed: bool = False,
) -> SubmissionOut:
    authoritative = next(
        (a for a in reversed(attempts) if a.state is AttemptState.COMPLETED), None
    )
    latest = attempts[-1] if attempts else None
    return SubmissionOut(
        id=submission.id,
        contest_id=submission.contest_id,
        problem_id=submission.problem_id,
        user_id=submission.user_id,
        language=submission.language,
        status=submission.status,
        submitted_at=submission.submitted_at,
        idempotency_key=submission.idempotency_key,
        replayed=replayed,
        authoritative_attempt=AttemptOut.model_validate(authoritative) if authoritative else None,
        latest_attempt=AttemptOut.model_validate(latest) if latest else None,
        source_code=submission.source_code if include_source else None,
        source_sha256=submission.source_sha256,
    )


@router.post(
    "/contests/{contest_id}/submissions",
    response_model=SubmissionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a program (participant). Idempotent on Idempotency-Key.",
    responses={
        200: {"description": "Replay: this key already created a submission"},
        409: {"description": "Key reused with different code or parameters, or a contest rule rejected it"},
    },
)
def create_submission(
    contest_id: uuid.UUID,
    body: SubmissionIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    principal: Principal = Depends(require_participant),
    session: Session = Depends(get_session),
) -> SubmissionOut:
    """Idempotency

    The key may be sent as the `Idempotency-Key` header or as `idempotency_key` in the body; if
    both are present they must agree. The key is scoped to (contest, actor), so two participants
    can use the same key safely and a key cannot be replayed into another contest.

    A replay returns 200 with the original submission. Reusing the key with different code, a
    different problem or a different language returns 409.
    """
    key = idempotency_key or body.idempotency_key
    if idempotency_key and body.idempotency_key and idempotency_key != body.idempotency_key:
        raise ValidationError("Idempotency-Key header and body idempotency_key disagree")
    if not key:
        raise ValidationError("an Idempotency-Key header or idempotency_key field is required")

    contest = access.get_contest(session, principal, contest_id)
    result = submission_service.create_submission(
        session,
        principal,
        contest,
        problem_id=body.problem_id,
        language=body.language,
        source_code=body.source_code,
        idempotency_key=key,
        request_id=x_request_id or current_request_id(),
    )
    attempts = _attempts_of(session, result.submission.id)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/submissions/{result.submission.id}"
    return _submission_out(
        result.submission, attempts, include_source=True, replayed=not result.created
    )


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionDetailOut,
    summary="Read one submission with its attempt history and per-test outcomes",
)
def get_submission(
    submission_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
) -> SubmissionDetailOut:
    """Per-test rows carry status and resource usage only: never the hidden input, the expected
    output or the program output."""
    submission, _ = access.get_submission(session, principal, submission_id)
    attempts = _attempts_of(session, submission.id)
    base = _submission_out(submission, attempts, include_source=True)
    authoritative = next((a for a in reversed(attempts) if a.state is AttemptState.COMPLETED), None)
    results: list[TestResultOut] = []
    if authoritative is not None:
        rows = session.execute(
            select(AttemptTestResult)
            .where(AttemptTestResult.attempt_id == authoritative.id)
            .order_by(AttemptTestResult.ordinal)
        ).scalars().all()
        results = [
            TestResultOut(
                ordinal=r.ordinal,
                is_sample=r.is_sample,
                status=r.status,
                time_ms=r.time_ms,
                memory_kb=r.memory_kb,
                output_bytes=r.output_bytes,
                truncated=r.truncated,
            )
            for r in rows
        ]
    return SubmissionDetailOut(
        **base.model_dump(),
        attempts=[AttemptOut.model_validate(a) for a in attempts],
        test_results=results,
    )


@router.get(
    "/contests/{contest_id}/submissions",
    response_model=PageEnvelope[SubmissionOut],
    summary="List submissions. Participants see only their own; organizers see all.",
)
def list_submissions(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
    user_id: uuid.UUID | None = Query(default=None),
    problem_id: uuid.UUID | None = Query(default=None),
    submission_status: SubmissionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PageEnvelope[SubmissionOut]:
    """Ordered by submitted_at descending then id descending, which is a total order."""
    contest = access.get_contest(session, principal, contest_id)
    filters = [Submission.contest_id == contest.id]
    if principal.is_participant:
        filters.append(Submission.user_id == principal.user_id)
    elif user_id is not None:
        filters.append(Submission.user_id == user_id)
    if problem_id is not None:
        filters.append(Submission.problem_id == problem_id)
    if submission_status is not None:
        filters.append(Submission.status == submission_status)

    total = session.execute(select(func.count(Submission.id)).where(*filters)).scalar_one()
    rows = session.execute(
        select(Submission)
        .where(*filters)
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    attempts_by_submission: dict[uuid.UUID, list[JudgingAttempt]] = {r.id: [] for r in rows}
    if rows:
        for attempt in session.execute(
            select(JudgingAttempt)
            .where(JudgingAttempt.submission_id.in_(list(attempts_by_submission)))
            .order_by(JudgingAttempt.attempt_no)
        ).scalars():
            attempts_by_submission[attempt.submission_id].append(attempt)
    return PageEnvelope[SubmissionOut](
        items=[
            _submission_out(
                row,
                attempts_by_submission[row.id],
                include_source=principal.is_organizer or row.user_id == principal.user_id,
            )
            for row in rows
        ],
        page=Page(limit=limit, offset=offset, total=total, returned=len(rows)),
    )


@router.get(
    "/submissions/{submission_id}/attempts",
    response_model=PageEnvelope[AttemptOrganizerOut],
    summary="Judge-level attempt detail (organizer): runtime config, worker, lease epoch, failures",
)
def list_attempts(
    submission_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> PageEnvelope[AttemptOrganizerOut]:
    submission, _ = access.get_submission(session, principal, submission_id)
    attempts = _attempts_of(session, submission.id)
    return PageEnvelope[AttemptOrganizerOut](
        items=[AttemptOrganizerOut.model_validate(a) for a in attempts],
        page=Page(limit=len(attempts), offset=0, total=len(attempts), returned=len(attempts)),
    )


@router.post(
    "/problems/{problem_id}/rejudge",
    response_model=BulkRejudgeOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rejudge every submission of a problem against a chosen version pair (organizer)",
)
def rejudge_problem(
    problem_id: uuid.UUID,
    body: RejudgeIn,
    user_id: uuid.UUID | None = Query(default=None, description="restrict to one participant"),
    limit: int = Query(default=500, ge=1, le=5000),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> BulkRejudgeOut:
    """Queues one new attempt per submission, oldest first.

    Each submission is rejudged in its own transaction, so a large rejudge does not hold one
    long-lived lock, and a failure part way through leaves the submissions it already queued
    correctly queued. Every new attempt is appended; nothing is overwritten, and each one
    becomes authoritative for its submission only once it completes.
    """
    problem, contest = access.get_problem(session, principal, problem_id)
    access.require_organizer_of(principal, contest)

    filters = [Submission.problem_id == problem.id]
    if user_id is not None:
        filters.append(Submission.user_id == user_id)
    rows = session.execute(
        select(Submission)
        .where(*filters)
        .order_by(Submission.submitted_at, Submission.id)
        .limit(limit)
    ).scalars().all()

    queued: list[uuid.UUID] = []
    for submission in rows:
        attempt = submission_service.rejudge(
            session,
            principal,
            submission,
            problem_version_id=body.problem_version_id,
            test_suite_id=body.test_suite_id,
            request_id=x_request_id or current_request_id(),
        )
        queued.append(attempt.id)
    return BulkRejudgeOut(
        problem_id=problem.id,
        submissions_matched=len(rows),
        attempts_queued=len(queued),
        problem_version_id=body.problem_version_id,
        test_suite_id=body.test_suite_id,
    )


@router.post(
    "/submissions/{submission_id}/rejudge",
    response_model=AttemptOrganizerOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rejudge against an explicitly chosen problem version and test suite (organizer)",
)
def rejudge(
    submission_id: uuid.UUID,
    body: RejudgeIn,
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> AttemptOrganizerOut:
    """The chosen pair must belong to the submission problem. Earlier attempts are preserved and
    remain readable; the new attempt becomes authoritative only once it completes."""
    submission, _ = access.get_submission(session, principal, submission_id)
    attempt = submission_service.rejudge(
        session,
        principal,
        submission,
        problem_version_id=body.problem_version_id,
        test_suite_id=body.test_suite_id,
        request_id=x_request_id,
    )
    return AttemptOrganizerOut.model_validate(attempt)
