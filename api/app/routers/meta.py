"""Health, version, queue visibility and failpoint control."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..auth import Principal, require_organizer
from ..config import get_settings
from ..db import get_session
from ..domain import failpoints
from ..domain.errors import AuthorizationError, ValidationError
from ..models import Failpoint
from ..runners import supported_languages
from ..schemas import FailpointIn
from ..services.judge_queue import queue_stats

router = APIRouter(tags=["meta"])
settings = get_settings()


@router.get("/healthz", summary="Liveness: the process is up")
def healthz() -> dict:
    return {"status": "ok", "service": settings.service_name}


@router.get("/readyz", summary="Readiness: the database answers")
def readyz(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/version", summary="Build and runtime labels that every result is tagged with")
def version() -> dict:
    return {
        "service": settings.service_name,
        "commit": settings.commit_sha,
        "execution_mode": settings.exec_mode,
        "lease_seconds": settings.lease_seconds,
        "max_deliveries": settings.max_deliveries,
        "languages": supported_languages(),
        "failpoints_armed": sorted(failpoints.armed()),
    }


@router.get("/ops/queue", summary="Judge queue depth by state (organizer)")
def queue(
    _: Principal = Depends(require_organizer), session: Session = Depends(get_session)
) -> dict:
    return {"execution_mode": settings.exec_mode, "queue": queue_stats(session)}


@router.post("/dev/failpoints", summary="Arm or disarm a failpoint (development builds only)")
def set_failpoint(
    body: FailpointIn,
    _: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> dict:
    """Deterministic fault injection for the recovery scenarios.

    Refused unless the development authentication fixture is enabled, so a production build
    cannot be told to break itself. The switch is stored in the database, so it reaches every
    API process within one refresh interval rather than only the one that served this request.
    """
    if not settings.dev_auth_enabled:
        raise AuthorizationError("failpoints are available only in development builds")
    if body.name not in failpoints.KNOWN:
        raise ValidationError(
            "unknown failpoint", details={"known": sorted(failpoints.KNOWN), "received": body.name}
        )
    session.execute(
        pg_insert(Failpoint)
        .values(name=body.name, enabled=body.enabled)
        .on_conflict_do_update(index_elements=["name"], set_={"enabled": body.enabled, "updated_at": func.now()})
    )
    session.commit()
    failpoints.invalidate()
    return {
        "armed": sorted(failpoints.armed()),
        "refresh_seconds": failpoints.REFRESH_SECONDS,
        "note": "other API processes pick this up within refresh_seconds",
    }
