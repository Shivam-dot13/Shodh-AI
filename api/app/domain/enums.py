from __future__ import annotations

import enum


class Role(str, enum.Enum):
    ORGANIZER = "ORGANIZER"
    PARTICIPANT = "PARTICIPANT"


class TokenKind(str, enum.Enum):
    USER = "USER"
    SERVICE = "SERVICE"


class Scope(str, enum.Enum):
    """Judge access is a separate auth plane from participant-facing API access."""

    USER = "user"
    JUDGE = "judge"
    PLATFORM_ADMIN = "platform_admin"


class ScoringMode(str, enum.Enum):
    ICPC = "ICPC"
    POINTS = "POINTS"


class Language(str, enum.Enum):
    PYTHON3 = "python3"
    CPP17 = "cpp17"


class CompareMode(str, enum.Enum):
    EXACT = "EXACT"
    TRIM_TRAILING_WS = "TRIM_TRAILING_WS"


class SubmissionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    JUDGED = "JUDGED"
    JUDGE_FAILED = "JUDGE_FAILED"


class AttemptKind(str, enum.Enum):
    INITIAL = "INITIAL"
    REJUDGE = "REJUDGE"


class AttemptState(str, enum.Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INFRA_FAILED = "INFRA_FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_ATTEMPT_STATES = {AttemptState.COMPLETED, AttemptState.INFRA_FAILED, AttemptState.CANCELLED}
LEASABLE_ATTEMPT_STATES = {AttemptState.PENDING, AttemptState.DISPATCHED, AttemptState.RUNNING}

ATTEMPT_TRANSITIONS: dict[AttemptState, set[AttemptState]] = {
    AttemptState.PENDING: {AttemptState.DISPATCHED, AttemptState.CANCELLED, AttemptState.INFRA_FAILED},
    AttemptState.DISPATCHED: {
        AttemptState.RUNNING,
        AttemptState.COMPLETED,
        AttemptState.INFRA_FAILED,
        AttemptState.PENDING,
        AttemptState.CANCELLED,
    },
    AttemptState.RUNNING: {
        AttemptState.COMPLETED,
        AttemptState.INFRA_FAILED,
        AttemptState.PENDING,
        AttemptState.CANCELLED,
    },
    AttemptState.COMPLETED: set(),
    AttemptState.INFRA_FAILED: set(),
    AttemptState.CANCELLED: set(),
}


def can_transition(src: AttemptState, dst: AttemptState) -> bool:
    return dst in ATTEMPT_TRANSITIONS.get(src, set())


class Verdict(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    WRONG_ANSWER = "WRONG_ANSWER"
    COMPILE_ERROR = "COMPILE_ERROR"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TIME_LIMIT_EXCEEDED = "TIME_LIMIT_EXCEEDED"
    MEMORY_LIMIT_EXCEEDED = "MEMORY_LIMIT_EXCEEDED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"


class TestStatus(str, enum.Enum):
    PASSED = "PASSED"
    WRONG_OUTPUT = "WRONG_OUTPUT"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TIMEOUT = "TIMEOUT"
    MEMORY_EXCEEDED = "MEMORY_EXCEEDED"
    OUTPUT_EXCEEDED = "OUTPUT_EXCEEDED"
    SKIPPED = "SKIPPED"


class JobState(str, enum.Enum):
    READY = "READY"
    LEASED = "LEASED"
    DONE = "DONE"
    DEAD = "DEAD"


class ExecMode(str, enum.Enum):
    DOCKER = "docker"
    LOCAL = "local"
