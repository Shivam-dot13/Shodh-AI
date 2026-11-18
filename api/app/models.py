"""Database schema.

The constraints here do the real work, not the application code:

  unique(submissions.contest_id, user_id, idempotency_key)   idempotent submission creation
  unique(judging_attempts.submission_id, attempt_no)         attempt history is append-only
  unique(judge_jobs.attempt_id)                              one queue row per attempt
  unique(problem_scores.contest_id, user_id, problem_id)     one score row per cell

Each attempt also pins problem_version_id, test_suite_id and runtime_config, so editing a
problem cannot change what an old verdict meant.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .domain.enums import (
    AttemptKind,
    AttemptState,
    CompareMode,
    JobState,
    Language,
    Role,
    ScoringMode,
    SubmissionStatus,
    TestStatus,
    TokenKind,
    Verdict,
)


class Base(DeclarativeBase):
    pass


def _enum(py_enum, name: str):
    # VARCHAR + CHECK rather than a native PG enum: same constraint, far cheaper migrations.
    return SAEnum(py_enum, native_enum=False, length=40, validate_strings=True, name=name)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_org", "org_id"),
    )
    organization: Mapped[Organization] = relationship()


class ApiToken(Base):
    """Development authentication fixture plus the separate judge service plane.

    Only a SHA-256 of the token is stored; the plaintext exists in the seed output only.
    """

    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[TokenKind] = mapped_column(_enum(TokenKind, "token_kind"), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    user: Mapped[User | None] = relationship()
    __table_args__ = (
        CheckConstraint(
            "(kind = 'SERVICE' AND user_id IS NULL) OR (kind = 'USER' AND user_id IS NOT NULL)",
            name="ck_api_tokens_kind_user",
        ),
    )


class Contest(Base):
    __tablename__ = "contests"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    scoring_mode: Mapped[ScoringMode] = mapped_column(
        _enum(ScoringMode, "scoring_mode"), nullable=False, default=ScoringMode.ICPC
    )
    penalty_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_attempts_per_problem: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Monotonic counter bumped in the same transaction as any standings change, so a client can
    # tell whether what it holds is current without trusting a clock.
    standings_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_contests_org_slug"),
        CheckConstraint("ends_at > starts_at", name="ck_contests_window"),
        CheckConstraint("penalty_minutes >= 0", name="ck_contests_penalty"),
        Index("ix_contests_org", "org_id"),
    )
    organization: Mapped[Organization] = relationship()


class ContestParticipant(Base):
    __tablename__ = "contest_participants"
    id: Mapped[uuid.UUID] = _pk()
    contest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contests.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    joined_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("contest_id", "user_id", name="uq_participant_contest_user"),
        Index("ix_participants_user", "user_id"),
    )
    user: Mapped[User] = relationship()


class Problem(Base):
    __tablename__ = "problems"
    id: Mapped[uuid.UUID] = _pk()
    contest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contests.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("contest_id", "slug", name="uq_problems_contest_slug"),
        Index("ix_problems_contest", "contest_id", "ordinal"),
    )
    contest: Mapped[Contest] = relationship()


class TestSuite(Base):
    """Test cases are versioned independently of the statement; a suite is immutable once used."""

    __tablename__ = "test_suites"
    id: Mapped[uuid.UUID] = _pk()
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("problem_id", "version", name="uq_test_suites_problem_version"),
        CheckConstraint("version >= 1", name="ck_test_suites_version"),
    )
    cases: Mapped[list[TestCase]] = relationship(
        back_populates="suite", order_by="TestCase.ordinal", cascade="all, delete-orphan"
    )


class TestCase(Base):
    __tablename__ = "test_cases"
    id: Mapped[uuid.UUID] = _pk()
    test_suite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_suites.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_data: Mapped[str] = mapped_column(Text, nullable=False)
    expected_output: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("test_suite_id", "ordinal", name="uq_test_cases_suite_ordinal"),
        CheckConstraint("weight >= 0", name="ck_test_cases_weight"),
    )
    suite: Mapped[TestSuite] = relationship(back_populates="cases")


class ProblemVersion(Base):
    __tablename__ = "problem_versions"
    id: Mapped[uuid.UUID] = _pk()
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    time_limit_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=256)
    output_limit_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=65536)
    max_points: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    compare_mode: Mapped[CompareMode] = mapped_column(
        _enum(CompareMode, "compare_mode"), nullable=False, default=CompareMode.TRIM_TRAILING_WS
    )
    default_test_suite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_suites.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("problem_id", "version", name="uq_problem_versions_problem_version"),
        CheckConstraint("version >= 1", name="ck_problem_versions_version"),
        CheckConstraint("time_limit_ms BETWEEN 100 AND 30000", name="ck_problem_versions_tl"),
        CheckConstraint("memory_limit_mb BETWEEN 16 AND 2048", name="ck_problem_versions_ml"),
        CheckConstraint("output_limit_bytes BETWEEN 256 AND 8388608", name="ck_problem_versions_ol"),
        CheckConstraint("max_points >= 0", name="ck_problem_versions_points"),
    )
    problem: Mapped[Problem] = relationship()
    default_test_suite: Mapped[TestSuite | None] = relationship()


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[uuid.UUID] = _pk()
    contest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contests.id", ondelete="CASCADE"), nullable=False
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[Language] = mapped_column(_enum(Language, "language"), nullable=False)
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # Hash of the semantically significant request fields; a reused key with a different
    # fingerprint is an explicit conflict rather than a silent no-op.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        _enum(SubmissionStatus, "submission_status"), nullable=False, default=SubmissionStatus.QUEUED
    )
    authoritative_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("contest_id", "user_id", "idempotency_key", name="uq_submissions_idempotency"),
        Index("ix_submissions_contest_user_problem", "contest_id", "user_id", "problem_id"),
        Index("ix_submissions_contest_listing", "contest_id", "submitted_at", "id"),
        Index("ix_submissions_problem", "problem_id"),
    )
    user: Mapped[User] = relationship()
    problem: Mapped[Problem] = relationship()
    contest: Mapped[Contest] = relationship()


class JudgingAttempt(Base):
    """One judging run of one submission. Append-only history; a rejudge adds an attempt."""

    __tablename__ = "judging_attempts"
    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[AttemptKind] = mapped_column(_enum(AttemptKind, "attempt_kind"), nullable=False)
    problem_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problem_versions.id", ondelete="RESTRICT"), nullable=False
    )
    test_suite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_suites.id", ondelete="RESTRICT"), nullable=False
    )
    # Frozen copy of the limits and runner identity in force for this attempt.
    runtime_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[AttemptState] = mapped_column(
        _enum(AttemptState, "attempt_state"), nullable=False, default=AttemptState.PENDING
    )
    verdict: Mapped[Verdict | None] = mapped_column(_enum(Verdict, "verdict"), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    runner_image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Fencing token: bumped on every lease. A result is accepted only with the current epoch,
    # so a worker whose lease expired cannot overwrite the result of a newer delivery.
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    compile_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    __table_args__ = (
        UniqueConstraint("submission_id", "attempt_no", name="uq_attempts_submission_no"),
        CheckConstraint("attempt_no >= 1", name="ck_attempts_no"),
        CheckConstraint("score >= 0 AND score <= max_score", name="ck_attempts_score_range"),
        CheckConstraint(
            "(state <> 'COMPLETED') OR (verdict IS NOT NULL)", name="ck_attempts_completed_has_verdict"
        ),
        CheckConstraint("(state <> 'INFRA_FAILED') OR (verdict IS NULL)", name="ck_attempts_infra_no_verdict"),
        Index("ix_attempts_submission", "submission_id", "attempt_no"),
        Index("ix_attempts_state", "state"),
    )
    submission: Mapped[Submission] = relationship()
    problem_version: Mapped[ProblemVersion] = relationship()
    test_suite: Mapped[TestSuite] = relationship()


class AttemptTestResult(Base):
    """Per test outcome. Deliberately holds no expected output and no program output."""

    __tablename__ = "attempt_test_results"
    id: Mapped[uuid.UUID] = _pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("judging_attempts.id", ondelete="CASCADE"), nullable=False
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[TestStatus] = mapped_column(_enum(TestStatus, "test_status"), nullable=False)
    time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint("attempt_id", "test_case_id", name="uq_results_attempt_case"),
        Index("ix_results_attempt", "attempt_id", "ordinal"),
    )


class JudgeJob(Base):
    """Outbox and work queue in one table.

    The row is inserted in the same transaction as the submission, so a committed submission
    always has a job. Claimed with SELECT ... FOR UPDATE SKIP LOCKED.
    """

    __tablename__ = "judge_jobs"
    id: Mapped[uuid.UUID] = _pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("judging_attempts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    state: Mapped[JobState] = mapped_column(_enum(JobState, "job_state"), nullable=False, default=JobState.READY)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    available_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_deliveries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        Index("ix_jobs_claim", "state", "priority", "available_at"),
        Index("ix_jobs_lease_expiry", "state", "lease_expires_at"),
        CheckConstraint("delivery_count >= 0", name="ck_jobs_delivery_count"),
    )
    attempt: Mapped[JudgingAttempt] = relationship()


class ProblemScore(Base):
    """One (contest, user, problem) cell of the standings.

    Recomputed from the authoritative attempts inside the verdict transaction. Derived, but
    never stale: there is no async refresh path.
    """

    __tablename__ = "problem_scores"
    id: Mapped[uuid.UUID] = _pk()
    contest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contests.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False
    )
    authoritative_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    penalty_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_before_solve: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    solved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    solved_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("contest_id", "user_id", "problem_id", name="uq_problem_scores_cell"),
        Index("ix_problem_scores_contest", "contest_id", "user_id"),
    )


class Failpoint(Base):
    """Fault-injection switches, kept in the database so every API process sees the same ones.

    Writes are refused unless the dev auth fixture is enabled.
    """

    __tablename__ = "failpoints"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = _pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(60), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id"),)
