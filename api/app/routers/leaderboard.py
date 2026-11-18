"""Leaderboard reads, plus the consistency and query-plan evidence routes."""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from ..auth import Principal, require_organizer, require_user
from ..config import get_settings
from ..db import get_session
from ..domain import failpoints
from ..domain.errors import AuthorizationError, ValidationError
from ..schemas import CellOut, LeaderboardOut, Page, StandingOut
from ..services import access, standings
from ..services.scores import rebuild_all_cells

router = APIRouter(tags=["leaderboard"])
settings = get_settings()

_SOURCES = {"standings", "naive", "rebuild"}


@router.get(
    "/contests/{contest_id}/leaderboard",
    response_model=LeaderboardOut,
    summary="Standings, with a selectable computation path",
)
def leaderboard(
    contest_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
    source: str = Query(default="", description="standings (default) | naive | rebuild"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100_000),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> LeaderboardOut | Response:
    """How clients observe updates

    The response carries `standings_version`, a counter bumped inside the transaction that
    stores a verdict, and an `ETag` identifying this representation: the version together with
    the source, limit and offset, so paging with `If-None-Match` cannot be answered with a page
    the client has never seen. A 304 means this exact page is unchanged. There is no
    asynchronous refresh anywhere in the write path, so a successful read is never behind a
    verdict that has already been stored.

    `source` selects the computation path and exists for evidence, not for tuning:
      standings  the maintained problem_scores cells (what everyone gets by default)
      naive      recompute from submissions and attempts on every request (pre-optimization)
      rebuild    full recompute in Python from authoritative records
    All three must return the same ranking; /standings/verify asserts it. The two re-deriving
    paths are organizer-only, because they cost far more than the serving path.
    """
    chosen = source or settings.default_standings_source
    if chosen not in _SOURCES:
        raise ValidationError(f"source must be one of {sorted(_SOURCES)}")
    contest = access.get_contest(session, principal, contest_id)

    if chosen != "standings" and not principal.is_organizer:
        # Both evidence paths re-derive the whole contest per request, so they cost one to two
        # orders of magnitude more than the serving path. Leaving them open to participants would
        # hand every one of them a cheap way to load the database.
        raise AuthorizationError(f"the {chosen} path is organizer-only; it re-derives the whole contest")

    # The entity tag has to identify the *representation*, not just the contest version: paging
    # with If-None-Match would otherwise answer 304 for a page the client has never seen.
    etag = f'W/"{contest.standings_version}-{chosen}-{limit}-{offset}"'
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag, "X-Standings-Source": chosen})

    failpoints.trip(failpoints.SLOW_LEADERBOARD, "injected leaderboard failure")
    started = time.perf_counter()
    if chosen == "naive":
        page = standings.naive(session, contest, limit=limit, offset=offset)
    elif chosen == "rebuild":
        page = standings.rebuild(session, contest)
        page.rows = page.rows[offset : offset + limit]
    else:
        page = standings.serve(session, contest, limit=limit, offset=offset)
    elapsed_ms = (time.perf_counter() - started) * 1000

    response.headers["ETag"] = etag
    response.headers["X-Standings-Source"] = page.source
    response.headers["X-Compute-Ms"] = f"{elapsed_ms:.2f}"
    response.headers["Cache-Control"] = "no-cache"
    return LeaderboardOut(
        contest_id=contest.id,
        scoring_mode=contest.scoring_mode,
        source=page.source,
        standings_version=page.standings_version,
        generated_at=page.generated_at,
        execution_mode=settings.exec_mode,
        items=[
            StandingOut(
                rank=row.rank,
                user_id=row.user_id,
                display_name=row.display_name,
                solved=row.solved,
                points=row.points,
                penalty_minutes=row.penalty_minutes,
                last_solved_at=row.last_solved_at,
                cells=[
                    CellOut(
                        problem_id=problem_id,
                        points=cell.points,
                        solved=cell.solved,
                        penalty_minutes=cell.penalty_minutes,
                        rejected_before_solve=cell.rejected_before_solve,
                        solved_at=cell.solved_at,
                    )
                    for problem_id, cell in sorted(row.cells.items(), key=lambda kv: str(kv[0]))
                ],
            )
            for row in page.rows
        ],
        page=Page(limit=limit, offset=offset, total=page.total, returned=len(page.rows)),
    )


@router.get(
    "/contests/{contest_id}/standings/verify",
    summary="Assert that the serving path, the naive path and a full rebuild agree (organizer)",
)
def verify_standings(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
    include_rows: bool = Query(default=False),
) -> dict:
    contest = access.get_contest(session, principal, contest_id)
    report = standings.verify(session, contest)
    if not include_rows:
        report.pop("serving", None)
        report.pop("rebuild", None)
        report.pop("naive", None)
    return report


@router.post(
    "/contests/{contest_id}/standings/rebuild",
    summary="Recompute every standings cell from authoritative records (organizer)",
)
def rebuild_standings(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> dict:
    """Used after a bulk rejudge, and as the repair path if a cell were ever suspected wrong.
    Writing it back must be a no-op on a healthy contest, which `verify` checks."""
    contest = access.get_contest(session, principal, contest_id)
    before = standings.comparable(standings.serve(session, contest, limit=10_000))
    cells = rebuild_all_cells(session, contest)
    session.commit()
    session.refresh(contest)
    after = standings.comparable(standings.serve(session, contest, limit=10_000))
    return {
        "contest_id": str(contest.id),
        "cells_written": cells,
        "changed": before != after,
        "standings_version": contest.standings_version,
    }


@router.get(
    "/contests/{contest_id}/standings/explain",
    summary="EXPLAIN (ANALYZE, BUFFERS) for a leaderboard path (organizer)",
)
def explain_standings(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
    source: str = Query(default="standings", description="standings | naive"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    if source not in {"standings", "naive"}:
        raise ValidationError("source must be standings or naive")
    contest = access.get_contest(session, principal, contest_id)
    plan = standings.explain(session, contest, source, limit=limit)
    return {"contest_id": str(contest.id), "source": source, "plan": plan.splitlines()}
