"""The judge plane: /internal/judge/*.

Every route here requires the `judge` service scope. A participant or organizer token is
rejected, and a judge token cannot read any participant-facing route. This is the only place
where hidden inputs and expected outputs leave the database, which is what makes the boundary
checkable rather than aspirational.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import Principal, require_judge
from ..config import get_settings
from ..db import get_session
from ..logging_setup import bind
from ..schemas import (
    AttemptOrganizerOut,
    HeartbeatIn,
    InfraFailureIn,
    LeaseEnvelope,
    LeaseOut,
    LeaseRequest,
    ResultIn,
)
from ..services import judge_queue

router = APIRouter(prefix="/internal/judge", tags=["judge plane"])
settings = get_settings()


@router.post("/leases", response_model=LeaseEnvelope, summary="Claim work (judge scope)")
def claim(
    body: LeaseRequest,
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> LeaseEnvelope:
    """Claims up to `capacity` jobs with SELECT ... FOR UPDATE SKIP LOCKED.

    Two workers calling this at the same instant cannot receive the same job: the row lock is
    held in the database, not in any process. Each lease carries a `lease_epoch` that must be
    presented with the result; the epoch is bumped on every lease and on every reclaim, so a
    stale worker is fenced out.
    """
    leases = judge_queue.lease_jobs(
        session, worker_id=body.worker_id, capacity=body.capacity, contest_id=body.contest_id
    )
    out = []
    for lease in leases:
        payload = judge_queue.build_payload(session, lease.attempt_id)
        out.append(
            LeaseOut(
                attempt_id=lease.attempt_id,
                lease_epoch=lease.lease_epoch,
                lease_expires_at=lease.lease_expires_at,
                delivery_count=lease.delivery_count,
                payload=payload,
            )
        )
    return LeaseEnvelope(leases=out, lease_seconds=settings.lease_seconds)


@router.get(
    "/attempts/{attempt_id}/payload",
    summary="Re-read the judging payload for a held lease (judge scope)",
)
def payload(
    attempt_id: uuid.UUID,
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> dict:
    """Lets a worker that restarted mid-job recover its input without a new delivery."""
    return judge_queue.build_payload(session, attempt_id)


@router.post("/attempts/{attempt_id}/heartbeat", summary="Extend a lease (judge scope)")
def heartbeat(
    attempt_id: uuid.UUID,
    body: HeartbeatIn,
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> dict:
    bind(attempt_id=str(attempt_id))
    expires = judge_queue.heartbeat(session, attempt_id, body.lease_epoch)
    return {"attempt_id": str(attempt_id), "lease_expires_at": expires.isoformat()}


@router.post(
    "/attempts/{attempt_id}/result",
    response_model=AttemptOrganizerOut,
    summary="Report a judging result (judge scope). Idempotent at a fixed lease epoch.",
    responses={409: {"description": "Stale lease epoch, or the attempt is already finished"}},
)
def result(
    attempt_id: uuid.UUID,
    body: ResultIn,
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> AttemptOrganizerOut:
    """What this call does, in one transaction

    stores the per-test outcomes, derives the verdict, moves the attempt to COMPLETED, retires
    the queue row, re-derives the submission status, and recomputes the one standings cell the
    verdict can affect. Either all of that is visible or none of it is.

    Replaying the same call with the same `lease_epoch` after a lost acknowledgement returns the
    stored attempt and changes nothing.
    """
    bind(attempt_id=str(attempt_id), worker_id=body.worker_id)
    attempt = judge_queue.apply_result(session, attempt_id, body.model_dump(mode="json"))
    return AttemptOrganizerOut.model_validate(attempt)


@router.post(
    "/attempts/{attempt_id}/infra-failure",
    summary="Report that the judge could not judge (judge scope)",
)
def infra_failure(
    attempt_id: uuid.UUID,
    body: InfraFailureIn,
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> dict:
    """Requeues within the delivery budget, otherwise marks the attempt INFRA_FAILED.

    Neither path writes a verdict or touches a score: a runner crash or an unavailable service
    is never a student error.
    """
    bind(attempt_id=str(attempt_id), worker_id=body.worker_id)
    return judge_queue.report_infra_failure(
        session,
        attempt_id,
        lease_epoch=body.lease_epoch,
        reason=body.reason,
        retryable=body.retryable,
    )


@router.post("/reap", summary="Reclaim expired leases now (judge scope)")
def reap(
    _: Principal = Depends(require_judge),
    session: Session = Depends(get_session),
) -> dict:
    """The API also does this on a timer; this route makes it deterministic in tests."""
    return judge_queue.reap_expired(session)
