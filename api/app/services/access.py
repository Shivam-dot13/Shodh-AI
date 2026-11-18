"""Tenant-scoped lookups.

Everything goes through these, so a route cannot load a contest, problem or submission without
saying who is asking.

  outside your organization            404
  inside it, wrong role                403
  another participant's submission     404, not even its existence
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal
from ..domain.errors import AuthorizationError, NotFoundError
from ..models import (
    Contest,
    ContestParticipant,
    JudgingAttempt,
    Problem,
    ProblemVersion,
    Submission,
    TestSuite,
)


def get_contest(session: Session, principal: Principal, contest_id: uuid.UUID) -> Contest:
    contest = session.get(Contest, contest_id)
    if contest is None or contest.org_id != principal.org_id:
        raise NotFoundError("contest not found")
    return contest


def get_problem(session: Session, principal: Principal, problem_id: uuid.UUID) -> tuple[Problem, Contest]:
    problem = session.get(Problem, problem_id)
    if problem is None:
        raise NotFoundError("problem not found")
    contest = get_contest(session, principal, problem.contest_id)
    return problem, contest


def get_problem_version(
    session: Session, principal: Principal, version_id: uuid.UUID
) -> tuple[ProblemVersion, Problem, Contest]:
    version = session.get(ProblemVersion, version_id)
    if version is None:
        raise NotFoundError("problem version not found")
    problem, contest = get_problem(session, principal, version.problem_id)
    return version, problem, contest


def get_test_suite(
    session: Session, principal: Principal, suite_id: uuid.UUID
) -> tuple[TestSuite, Problem, Contest]:
    suite = session.get(TestSuite, suite_id)
    if suite is None:
        raise NotFoundError("test suite not found")
    problem, contest = get_problem(session, principal, suite.problem_id)
    return suite, problem, contest


def get_submission(
    session: Session, principal: Principal, submission_id: uuid.UUID
) -> tuple[Submission, Contest]:
    """Participants may read only their own submissions; organizers, any in their org."""
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError("submission not found")
    contest = session.get(Contest, submission.contest_id)
    if contest is None or contest.org_id != principal.org_id:
        raise NotFoundError("submission not found")
    if principal.is_participant and submission.user_id != principal.user_id:
        # Same tenant, but another participant private work: hide its existence as well.
        raise NotFoundError("submission not found")
    if not principal.is_participant and not principal.is_organizer:
        raise AuthorizationError("user identity required")
    return submission, contest


def get_attempt(
    session: Session, principal: Principal, attempt_id: uuid.UUID
) -> tuple[JudgingAttempt, Submission, Contest]:
    attempt = session.get(JudgingAttempt, attempt_id)
    if attempt is None:
        raise NotFoundError("attempt not found")
    submission, contest = get_submission(session, principal, attempt.submission_id)
    return attempt, submission, contest


def is_participant_of(session: Session, contest_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        session.execute(
            select(ContestParticipant.id).where(
                ContestParticipant.contest_id == contest_id, ContestParticipant.user_id == user_id
            )
        ).scalar_one_or_none()
        is not None
    )


def require_organizer_of(principal: Principal, contest: Contest) -> None:
    if not principal.is_organizer:
        raise AuthorizationError("organizer role required")
    if contest.org_id != principal.org_id:  # defence in depth; get_contest already filtered
        raise NotFoundError("contest not found")
