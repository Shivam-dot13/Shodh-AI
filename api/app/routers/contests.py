"""Contests, participation, problems, problem versions and test suites.

Versioning rule that the rest of the system leans on: a problem version and a test suite are
immutable once created. Changing a problem means adding a version (and usually a suite), which
is why a historical verdict keeps its meaning - the attempt that produced it points at the
exact pair that was used.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Principal, require_organizer, require_participant, require_user
from ..db import get_session
from ..domain.errors import BusinessRuleError, NotFoundError, ValidationError
from ..models import (
    Contest,
    ContestParticipant,
    Problem,
    ProblemVersion,
    Submission,
    TestCase,
    TestSuite,
    User,
)
from ..schemas import (
    ContestIn,
    ContestOut,
    ContestPatch,
    Page,
    PageEnvelope,
    ParticipantOut,
    ProblemDetailOut,
    ProblemIn,
    ProblemOut,
    ProblemVersionDetail,
    ProblemVersionIn,
    ProblemVersionOut,
    TestSuiteIn,
    TestSuiteOut,
)
from ..services import access

router = APIRouter(tags=["contests"])


def _page(limit: int, offset: int, total: int, returned: int) -> Page:
    return Page(limit=limit, offset=offset, total=total, returned=returned)


def _contest_out(contest: Contest) -> ContestOut:
    return ContestOut.model_validate(contest)


@router.get(
    "/contests",
    response_model=PageEnvelope[ContestOut],
    summary="List contests of the caller organization",
)
def list_contests(
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PageEnvelope[ContestOut]:
    """Ordered by starts_at descending, then id, so paging is stable under concurrent writes."""
    total = session.execute(
        select(func.count(Contest.id)).where(Contest.org_id == principal.org_id)
    ).scalar_one()
    rows = session.execute(
        select(Contest)
        .where(Contest.org_id == principal.org_id)
        .order_by(Contest.starts_at.desc(), Contest.id)
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return PageEnvelope[ContestOut](
        items=[_contest_out(c) for c in rows], page=_page(limit, offset, total, len(rows))
    )


@router.post(
    "/contests",
    response_model=ContestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a contest (organizer)",
)
def create_contest(
    body: ContestIn,
    response: Response,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> ContestOut:
    contest = Contest(
        id=uuid.uuid4(),
        org_id=principal.org_id,
        slug=body.slug,
        title=body.title,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        scoring_mode=body.scoring_mode,
        penalty_minutes=body.penalty_minutes,
        max_attempts_per_problem=body.max_attempts_per_problem,
    )
    session.add(contest)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("contest slug already used in this organization", code="duplicate_slug") from exc
    response.headers["Location"] = f"/contests/{contest.id}"
    return _contest_out(contest)


@router.get("/contests/{contest_id}", response_model=ContestOut, summary="Read one contest")
def get_contest(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
) -> ContestOut:
    return _contest_out(access.get_contest(session, principal, contest_id))


@router.patch("/contests/{contest_id}", response_model=ContestOut, summary="Amend a contest (organizer)")
def patch_contest(
    contest_id: uuid.UUID,
    body: ContestPatch,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> ContestOut:
    contest = access.get_contest(session, principal, contest_id)
    access.require_organizer_of(principal, contest)
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(contest, field, value)
    if contest.ends_at <= contest.starts_at:
        raise ValidationError("ends_at must be after starts_at")
    session.commit()
    return _contest_out(contest)


@router.post(
    "/contests/{contest_id}/participants/me",
    response_model=ParticipantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Join a contest (participant). Idempotent.",
)
def join_contest(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_participant),
    session: Session = Depends(get_session),
) -> ParticipantOut:
    contest = access.get_contest(session, principal, contest_id)
    if datetime.now(timezone.utc) >= contest.ends_at:
        raise BusinessRuleError("contest has closed", code="contest_closed")
    row = ContestParticipant(id=uuid.uuid4(), contest_id=contest.id, user_id=principal.user_id)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        row = session.execute(
            select(ContestParticipant).where(
                ContestParticipant.contest_id == contest.id,
                ContestParticipant.user_id == principal.user_id,
            )
        ).scalar_one()
    user = session.get(User, principal.user_id)
    assert user is not None
    return ParticipantOut(
        user_id=user.id, display_name=user.display_name, email=user.email, joined_at=row.joined_at
    )


@router.get(
    "/contests/{contest_id}/participants",
    response_model=PageEnvelope[ParticipantOut],
    summary="List participants (organizer)",
)
def list_participants(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PageEnvelope[ParticipantOut]:
    contest = access.get_contest(session, principal, contest_id)
    total = session.execute(
        select(func.count(ContestParticipant.id)).where(ContestParticipant.contest_id == contest.id)
    ).scalar_one()
    rows = session.execute(
        select(ContestParticipant, User)
        .join(User, User.id == ContestParticipant.user_id)
        .where(ContestParticipant.contest_id == contest.id)
        .order_by(ContestParticipant.joined_at, ContestParticipant.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return PageEnvelope[ParticipantOut](
        items=[
            ParticipantOut(
                user_id=user.id,
                display_name=user.display_name,
                email=user.email,
                joined_at=cp.joined_at,
            )
            for cp, user in rows
        ],
        page=_page(limit, offset, total, len(rows)),
    )


# --------------------------------------------------------------------------- problems


def _lock_problem(session: Session, problem: Problem) -> None:
    """Serialise version numbering for one problem.

    `max(version) + 1` read without a lock lets two concurrent organizers pick the same number;
    the unique constraint then rejects one of them with a generic constraint error and the
    organizer has to work out that a retry is what was needed. Locking the problem row first
    makes the numbering correct instead of merely safe, and matches how `rejudge` numbers
    attempts.
    """
    session.execute(select(Problem.id).where(Problem.id == problem.id).with_for_update()).one()


def _create_suite(session: Session, problem: Problem, body: TestSuiteIn) -> TestSuite:
    _lock_problem(session, problem)
    next_version = (
        session.execute(
            select(func.max(TestSuite.version)).where(TestSuite.problem_id == problem.id)
        ).scalar_one()
        or 0
    ) + 1
    suite = TestSuite(
        id=uuid.uuid4(), problem_id=problem.id, version=next_version, label=body.label
    )
    session.add(suite)
    for case in body.cases:
        session.add(
            TestCase(
                id=uuid.uuid4(),
                test_suite_id=suite.id,
                ordinal=case.ordinal,
                is_sample=case.is_sample,
                weight=case.weight,
                input_data=case.input,
                expected_output=case.expected_output,
            )
        )
    session.flush()
    return suite


def _create_version(
    session: Session, problem: Problem, body: ProblemVersionIn
) -> ProblemVersion:
    if body.test_suite is None and body.test_suite_id is None:
        raise ValidationError("provide either test_suite (inline) or test_suite_id")
    if body.test_suite is not None and body.test_suite_id is not None:
        raise ValidationError("provide only one of test_suite or test_suite_id")
    if body.test_suite is not None:
        suite = _create_suite(session, problem, body.test_suite)
    else:
        suite = session.get(TestSuite, body.test_suite_id)
        if suite is None or suite.problem_id != problem.id:
            raise ValidationError("test_suite_id does not belong to this problem")

    _lock_problem(session, problem)
    next_version = (
        session.execute(
            select(func.max(ProblemVersion.version)).where(ProblemVersion.problem_id == problem.id)
        ).scalar_one()
        or 0
    ) + 1
    version = ProblemVersion(
        id=uuid.uuid4(),
        problem_id=problem.id,
        version=next_version,
        statement=body.statement,
        time_limit_ms=body.time_limit_ms,
        memory_limit_mb=body.memory_limit_mb,
        output_limit_bytes=body.output_limit_bytes,
        max_points=body.max_points,
        compare_mode=body.compare_mode,
        default_test_suite_id=suite.id,
    )
    session.add(version)
    session.flush()
    if body.publish:
        problem.current_version_id = version.id
    return version


@router.post(
    "/contests/{contest_id}/problems",
    response_model=ProblemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a problem with its first version and test suite (organizer)",
)
def create_problem(
    contest_id: uuid.UUID,
    body: ProblemIn,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> ProblemOut:
    contest = access.get_contest(session, principal, contest_id)
    access.require_organizer_of(principal, contest)
    problem = Problem(
        id=uuid.uuid4(), contest_id=contest.id, slug=body.slug, title=body.title, ordinal=body.ordinal
    )
    session.add(problem)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("problem slug already used in this contest", code="duplicate_slug") from exc
    version = _create_version(session, problem, body.version)
    session.commit()
    return ProblemOut(
        id=problem.id,
        contest_id=problem.contest_id,
        slug=problem.slug,
        title=problem.title,
        ordinal=problem.ordinal,
        current_version_id=problem.current_version_id,
        current_version=ProblemVersionOut.model_validate(version),
    )


@router.get(
    "/contests/{contest_id}/problems",
    response_model=PageEnvelope[ProblemOut],
    summary="List problems of a contest, ordered by ordinal then id",
)
def list_problems(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PageEnvelope[ProblemOut]:
    contest = access.get_contest(session, principal, contest_id)
    if principal.is_participant and datetime.now(timezone.utc) < contest.starts_at:
        raise BusinessRuleError("problems are published when the contest opens", code="contest_not_started")
    total = session.execute(
        select(func.count(Problem.id)).where(Problem.contest_id == contest.id)
    ).scalar_one()
    rows = session.execute(
        select(Problem)
        .where(Problem.contest_id == contest.id)
        .order_by(Problem.ordinal, Problem.id)
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    versions = {
        v.id: v
        for v in session.execute(
            select(ProblemVersion).where(
                ProblemVersion.id.in_([p.current_version_id for p in rows if p.current_version_id])
            )
        ).scalars()
    }
    return PageEnvelope[ProblemOut](
        items=[
            ProblemOut(
                id=p.id,
                contest_id=p.contest_id,
                slug=p.slug,
                title=p.title,
                ordinal=p.ordinal,
                current_version_id=p.current_version_id,
                current_version=(
                    ProblemVersionOut.model_validate(versions[p.current_version_id])
                    if p.current_version_id in versions
                    else None
                ),
            )
            for p in rows
        ],
        page=_page(limit, offset, total, len(rows)),
    )


@router.get(
    "/problems/{problem_id}",
    response_model=ProblemDetailOut,
    summary="Read a problem. Participants get the statement and sample tests only.",
)
def get_problem(
    problem_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: Session = Depends(get_session),
) -> ProblemDetailOut:
    problem, contest = access.get_problem(session, principal, problem_id)
    if principal.is_participant and datetime.now(timezone.utc) < contest.starts_at:
        raise BusinessRuleError("problems are published when the contest opens", code="contest_not_started")
    version = (
        session.get(ProblemVersion, problem.current_version_id) if problem.current_version_id else None
    )
    samples: list[dict] = []
    if version is not None and version.default_test_suite_id is not None:
        rows = session.execute(
            select(TestCase)
            .where(TestCase.test_suite_id == version.default_test_suite_id, TestCase.is_sample.is_(True))
            .order_by(TestCase.ordinal)
        ).scalars().all()
        samples = [
            {"ordinal": c.ordinal, "input": c.input_data, "expected_output": c.expected_output}
            for c in rows
        ]
    return ProblemDetailOut(
        id=problem.id,
        contest_id=problem.contest_id,
        slug=problem.slug,
        title=problem.title,
        ordinal=problem.ordinal,
        current_version_id=problem.current_version_id,
        current_version=ProblemVersionOut.model_validate(version) if version else None,
        statement=version.statement if version else None,
        sample_tests=samples,
    )


@router.post(
    "/problems/{problem_id}/versions",
    response_model=ProblemVersionDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new problem version (organizer). Historical verdicts are untouched.",
)
def create_problem_version(
    problem_id: uuid.UUID,
    body: ProblemVersionIn,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> ProblemVersionDetail:
    problem, contest = access.get_problem(session, principal, problem_id)
    access.require_organizer_of(principal, contest)
    version = _create_version(session, problem, body)
    session.commit()
    return ProblemVersionDetail.model_validate(version)


@router.get(
    "/problems/{problem_id}/versions",
    response_model=PageEnvelope[ProblemVersionOut],
    summary="List problem versions (organizer)",
)
def list_problem_versions(
    problem_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> PageEnvelope[ProblemVersionOut]:
    problem, contest = access.get_problem(session, principal, problem_id)
    access.require_organizer_of(principal, contest)
    rows = session.execute(
        select(ProblemVersion)
        .where(ProblemVersion.problem_id == problem.id)
        .order_by(ProblemVersion.version)
    ).scalars().all()
    return PageEnvelope[ProblemVersionOut](
        items=[ProblemVersionOut.model_validate(v) for v in rows],
        page=_page(len(rows), 0, len(rows), len(rows)),
    )


@router.post(
    "/problems/{problem_id}/test-suites",
    response_model=TestSuiteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a test suite version (organizer). Hidden data is never returned.",
)
def create_test_suite(
    problem_id: uuid.UUID,
    body: TestSuiteIn,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> TestSuiteOut:
    problem, contest = access.get_problem(session, principal, problem_id)
    access.require_organizer_of(principal, contest)
    suite = _create_suite(session, problem, body)
    session.commit()
    return TestSuiteOut(
        id=suite.id,
        problem_id=problem.id,
        version=suite.version,
        label=suite.label,
        case_count=len(body.cases),
        created_at=suite.created_at,
    )


@router.get(
    "/problems/{problem_id}/test-suites",
    response_model=PageEnvelope[TestSuiteOut],
    summary="List test suite versions and their sizes (organizer). Case contents stay hidden.",
)
def list_test_suites(
    problem_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> PageEnvelope[TestSuiteOut]:
    problem, contest = access.get_problem(session, principal, problem_id)
    access.require_organizer_of(principal, contest)
    rows = session.execute(
        select(TestSuite, func.count(TestCase.id))
        .outerjoin(TestCase, TestCase.test_suite_id == TestSuite.id)
        .where(TestSuite.problem_id == problem.id)
        .group_by(TestSuite.id)
        .order_by(TestSuite.version)
    ).all()
    return PageEnvelope[TestSuiteOut](
        items=[
            TestSuiteOut(
                id=suite.id,
                problem_id=problem.id,
                version=suite.version,
                label=suite.label,
                case_count=int(count),
                created_at=suite.created_at,
            )
            for suite, count in rows
        ],
        page=_page(len(rows), 0, len(rows), len(rows)),
    )


@router.get(
    "/contests/{contest_id}/stats",
    summary="Contest counters an organizer can sanity-check against the leaderboard",
)
def contest_stats(
    contest_id: uuid.UUID,
    principal: Principal = Depends(require_organizer),
    session: Session = Depends(get_session),
) -> dict:
    contest = access.get_contest(session, principal, contest_id)
    submissions = session.execute(
        select(func.count(Submission.id)).where(Submission.contest_id == contest.id)
    ).scalar_one()
    participants = session.execute(
        select(func.count(ContestParticipant.id)).where(ContestParticipant.contest_id == contest.id)
    ).scalar_one()
    problems = session.execute(
        select(func.count(Problem.id)).where(Problem.contest_id == contest.id)
    ).scalar_one()
    return {
        "contest_id": str(contest.id),
        "participants": participants,
        "problems": problems,
        "submissions": submissions,
        "standings_version": contest.standings_version,
    }
