"""Application wiring: middleware, error shape, the lease reaper, OpenAPI metadata."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from sqlalchemy import text
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError

from .config import get_settings
from .db import SessionLocal
from .domain import failpoints
from .domain.errors import AppError, InfrastructureError, ValidationError
from .logging_setup import bind, clear, current_request_id, setup_logging
from .routers import contests, identities, judge, leaderboard, meta, submissions
from .services.judge_queue import reap_expired

settings = get_settings()
setup_logging(settings.log_level, settings.service_name)
failpoints.load(settings.failpoint_set)
log = logging.getLogger("shodh.api")


def _load_shared_failpoints() -> set[str]:
    """Read the shared failpoint switches so every API process honours the same ones."""
    session = SessionLocal()
    try:
        rows = session.execute(
            text("SELECT name FROM failpoints WHERE enabled IS TRUE")
        ).scalars().all()
        return set(rows)
    finally:
        session.close()


failpoints.set_loader(_load_shared_failpoints)

DESCRIPTION = """
Backend for **Shodh a Code**, a coding contest platform with an asynchronous judge.

Authentication
    Bearer tokens. The seed prints one per identity (see `evidence/seed-manifest.json` or the
    output of `make seed`). `POST /auth/dev/token` mints a token for any identity; it is a
    development fixture and is refused when `DEV_AUTH_ENABLED=false`.

Three credential scopes
    `user` participants and organizers, pinned to one organization
    `judge` the worker plane under `/internal/judge/*`
    `platform_admin` bootstrap of organizations and identities

Error shape
    Every failure is `{"error": {"type", "code", "message", "details", "request_id"}}`, where
    `type` is one of `validation` (422), `business_rule` (409), `authentication` (401),
    `authorization` (403), `not_found` (404) or `infrastructure` (503). A cross-tenant read is
    a 404 by design: knowing an identifier must not confirm that it exists.

Correlation
    Send `X-Request-Id` and it is echoed, stored on the submission, carried into the worker and
    printed in every structured log line for the operation.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()

    async def reaper() -> None:
        """Recover work abandoned by a worker that died holding a lease."""
        while not stop.is_set():
            try:
                await anyio.to_thread.run_sync(_reap_once)
            except Exception:  # pragma: no cover - a reaper must never kill the process
                log.exception("reaper iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.reaper_interval_seconds)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(reaper()) if settings.reaper_enabled else None
    log.info(
        "api started",
        extra={"execution_mode": settings.exec_mode, "reaper": bool(task), "commit": settings.commit_sha},
    )
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            task.cancel()


def _reap_once() -> None:
    session = SessionLocal()
    try:
        reap_expired(session)
    finally:
        session.close()


app = FastAPI(
    title="Shodh a Code - contest platform backend",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "meta", "description": "Health, build labels, queue depth, failpoints."},
        {"name": "identities", "description": "Organizations, identities and the dev auth fixture."},
        {"name": "contests", "description": "Contests, participation, problems, versions, suites."},
        {"name": "submissions", "description": "Submitting, observing and rejudging."},
        {"name": "leaderboard", "description": "Standings and the consistency evidence routes."},
        {"name": "judge plane", "description": "Worker-only protocol. Separate credential scope."},
    ],
)


@app.middleware("http")
async def correlate(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    clear()
    bind(request_id=request_id, method=request.method, path=request.url.path)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"duration_ms": (time.perf_counter() - started) * 1000})
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Execution-Mode"] = settings.exec_mode
    logging.getLogger("shodh.access").info(
        "request completed",
        extra={"status_code": response.status_code, "duration_ms": round(duration_ms, 2)},
    )
    clear()
    return response


def _error_response(exc: AppError, request: Request) -> JSONResponse:
    # The generated id lives in the logging context, not on request.state; without this fallback
    # an error raised by a client that sent no X-Request-Id would carry request_id: null in the
    # body while the response header carried the real one, and the two would not match.
    request_id = request.headers.get("x-request-id") or current_request_id()
    return JSONResponse(status_code=exc.status_code, content=exc.body(request_id))


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    level = logging.WARNING if exc.status_code < 500 else logging.ERROR
    log.log(
        level,
        "request rejected",
        extra={"error_type": exc.error_type, "error_code": exc.code, "status_code": exc.status_code},
    )
    return _error_response(exc, request)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Request shape problems are 422 and never confused with a contest-rule rejection."""
    details = [
        {"location": list(err.get("loc", [])), "message": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return _error_response(ValidationError("request failed validation", details=details), request)


@app.exception_handler(IntegrityError)
async def integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    log.warning("database constraint rejected a write", extra={"constraint_error": exc.__class__.__name__})
    from .domain.errors import BusinessRuleError

    return _error_response(
        BusinessRuleError("the write violated a uniqueness or integrity constraint", code="constraint_violation"),
        request,
    )


@app.exception_handler(OperationalError)
async def operational_handler(request: Request, exc: OperationalError) -> JSONResponse:
    log.error("database unavailable", extra={"db_error": exc.__class__.__name__})
    return _error_response(InfrastructureError("database unavailable"), request)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled exception")
    return _error_response(InfrastructureError("internal error"), request)


app.include_router(meta.router)
app.include_router(identities.router)
app.include_router(contests.router)
app.include_router(submissions.router)
app.include_router(leaderboard.router)
app.include_router(judge.router)
