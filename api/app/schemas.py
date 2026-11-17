"""Request and response contracts.

Validation lives here and nowhere else, which is what keeps request-shape errors (422) apart
from contest-rule errors (409). List responses are always {"items", "page"} with a stable
ordering stated in the route docstring.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain.enums import (
    AttemptKind,
    AttemptState,
    CompareMode,
    Language,
    Role,
    ScoringMode,
    SubmissionStatus,
    TestStatus,
    Verdict,
)

T = TypeVar("T")


class Page(BaseModel):
    limit: int
    offset: int
    total: int
    returned: int


class PageEnvelope(BaseModel, Generic[T]):
    items: list[T]
    page: Page


class PaginationParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


# --------------------------------------------------------------------------- identities


class OrganizationIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=200)


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    role: Role

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must contain a local part and a domain")
        return value.lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    display_name: str
    role: Role


class UserWithTokenOut(UserOut):
    token: str | None = None


class TokenRequest(BaseModel):
    email: str


class TokenOut(BaseModel):
    token: str
    user: UserOut


class MeOut(BaseModel):
    scope: str
    label: str
    user: UserOut | None = None


# --------------------------------------------------------------------------- contests


class ContestIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=2, max_length=200)
    starts_at: datetime
    ends_at: datetime
    scoring_mode: ScoringMode = ScoringMode.ICPC
    penalty_minutes: int = Field(default=20, ge=0, le=600)
    max_attempts_per_problem: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("ends_at")
    @classmethod
    def _window(cls, value: datetime, info):  # type: ignore[no-untyped-def]
        starts = info.data.get("starts_at")
        if starts is not None and value <= starts:
            raise ValueError("ends_at must be after starts_at")
        return value


class ContestPatch(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    penalty_minutes: int | None = Field(default=None, ge=0, le=600)
    max_attempts_per_problem: int | None = Field(default=None, ge=1, le=1000)


class ContestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    org_id: uuid.UUID
    slug: str
    title: str
    starts_at: datetime
    ends_at: datetime
    scoring_mode: ScoringMode
    penalty_minutes: int
    max_attempts_per_problem: int | None
    standings_version: int


class ParticipantOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    joined_at: datetime


# --------------------------------------------------------------------------- problems


class TestCaseIn(BaseModel):
    ordinal: int = Field(ge=1, le=1000)
    input: str = Field(max_length=256 * 1024)
    expected_output: str = Field(max_length=256 * 1024)
    is_sample: bool = False
    weight: int = Field(default=1, ge=0, le=1000)


class TestSuiteIn(BaseModel):
    label: str = Field(default="", max_length=120)
    cases: list[TestCaseIn] = Field(min_length=1, max_length=200)

    @field_validator("cases")
    @classmethod
    def _unique_ordinals(cls, value: list[TestCaseIn]) -> list[TestCaseIn]:
        ordinals = [c.ordinal for c in value]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("test case ordinals must be unique")
        return value


class TestSuiteOut(BaseModel):
    id: uuid.UUID
    problem_id: uuid.UUID
    version: int
    label: str
    case_count: int
    created_at: datetime


class ProblemVersionIn(BaseModel):
    statement: str = Field(default="", max_length=64 * 1024)
    time_limit_ms: int = Field(default=2000, ge=100, le=30_000)
    memory_limit_mb: int = Field(default=256, ge=16, le=2048)
    output_limit_bytes: int = Field(default=65_536, ge=256, le=8 * 1024 * 1024)
    max_points: int = Field(default=100, ge=0, le=100_000)
    compare_mode: CompareMode = CompareMode.TRIM_TRAILING_WS
    test_suite: TestSuiteIn | None = None
    test_suite_id: uuid.UUID | None = None
    publish: bool = True


class ProblemVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    problem_id: uuid.UUID
    version: int
    time_limit_ms: int
    memory_limit_mb: int
    output_limit_bytes: int
    max_points: int
    compare_mode: CompareMode
    default_test_suite_id: uuid.UUID | None
    created_at: datetime


class ProblemVersionDetail(ProblemVersionOut):
    statement: str


class ProblemIn(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(default=1, ge=1, le=1000)
    version: ProblemVersionIn


class ProblemOut(BaseModel):
    id: uuid.UUID
    contest_id: uuid.UUID
    slug: str
    title: str
    ordinal: int
    current_version_id: uuid.UUID | None
    current_version: ProblemVersionOut | None = None


class ProblemDetailOut(ProblemOut):
    statement: str | None = None
    sample_tests: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- submissions


class SubmissionIn(BaseModel):
    problem_id: uuid.UUID
    language: Language
    source_code: str = Field(min_length=1, max_length=256 * 1024)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    attempt_no: int
    kind: AttemptKind
    state: AttemptState
    verdict: Verdict | None
    score: int
    max_score: int
    tests_passed: int
    tests_total: int
    time_ms: int | None
    memory_kb: int | None
    exec_mode: str | None
    compile_message: str | None = None
    problem_version_id: uuid.UUID
    test_suite_id: uuid.UUID
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AttemptOrganizerOut(AttemptOut):
    runtime_config: dict
    runner_image: str | None
    worker_id: str | None
    lease_epoch: int
    failure_reason: str | None
    compile_message: str | None
    request_id: str | None


class TestResultOut(BaseModel):
    ordinal: int
    is_sample: bool
    status: TestStatus
    time_ms: int | None
    memory_kb: int | None
    output_bytes: int
    truncated: bool


class SubmissionOut(BaseModel):
    id: uuid.UUID
    contest_id: uuid.UUID
    problem_id: uuid.UUID
    user_id: uuid.UUID
    language: Language
    status: SubmissionStatus
    submitted_at: datetime
    idempotency_key: str
    replayed: bool = False
    authoritative_attempt: AttemptOut | None = None
    latest_attempt: AttemptOut | None = None
    source_code: str | None = None
    source_sha256: str | None = None


class SubmissionDetailOut(SubmissionOut):
    attempts: list[AttemptOut] = Field(default_factory=list)
    test_results: list[TestResultOut] = Field(default_factory=list)


class RejudgeIn(BaseModel):
    problem_version_id: uuid.UUID
    test_suite_id: uuid.UUID


class BulkRejudgeOut(BaseModel):
    problem_id: uuid.UUID
    submissions_matched: int
    attempts_queued: int
    problem_version_id: uuid.UUID
    test_suite_id: uuid.UUID


# --------------------------------------------------------------------------- leaderboard


class CellOut(BaseModel):
    problem_id: uuid.UUID
    points: int
    solved: bool
    penalty_minutes: int
    rejected_before_solve: int
    solved_at: datetime | None


class StandingOut(BaseModel):
    rank: int
    user_id: uuid.UUID
    display_name: str
    solved: int
    points: int
    penalty_minutes: int
    last_solved_at: datetime | None
    cells: list[CellOut] = Field(default_factory=list)


class LeaderboardOut(BaseModel):
    contest_id: uuid.UUID
    scoring_mode: ScoringMode
    source: str
    standings_version: int
    generated_at: datetime
    execution_mode: str
    items: list[StandingOut]
    page: Page


# --------------------------------------------------------------------------- judge plane


class LeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=80)
    capacity: int = Field(default=1, ge=1, le=16)


class LeaseOut(BaseModel):
    attempt_id: uuid.UUID
    lease_epoch: int
    lease_expires_at: datetime
    delivery_count: int
    payload: dict


class LeaseEnvelope(BaseModel):
    leases: list[LeaseOut]
    lease_seconds: int


class HeartbeatIn(BaseModel):
    lease_epoch: int = Field(ge=0)


class TestReportIn(BaseModel):
    test_case_id: uuid.UUID
    status: TestStatus
    time_ms: int | None = Field(default=None, ge=0, le=600_000)
    memory_kb: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    output_bytes: int = Field(default=0, ge=0)
    truncated: bool = False


class CompileReportIn(BaseModel):
    ok: bool = True
    message: str = Field(default="", max_length=8192)
    time_ms: int | None = Field(default=None, ge=0)


class ResultIn(BaseModel):
    lease_epoch: int = Field(ge=0)
    worker_id: str = Field(min_length=1, max_length=80)
    exec_mode: str = Field(min_length=1, max_length=16)
    runner_image: str | None = Field(default=None, max_length=200)
    compile: CompileReportIn = Field(default_factory=CompileReportIn)
    tests: list[TestReportIn] = Field(default_factory=list, max_length=500)
    time_ms: int | None = Field(default=None, ge=0)
    memory_kb: int | None = Field(default=None, ge=0)


class InfraFailureIn(BaseModel):
    lease_epoch: int = Field(ge=0)
    worker_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=2000)
    retryable: bool = True


# --------------------------------------------------------------------------- ops


class FailpointIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
