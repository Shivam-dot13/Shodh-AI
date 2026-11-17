"""Output comparison and the mapping from runner observations to a test status.

Both live in the worker, not in the sandbox: the expected output never enters the container
that runs submitted code. The API then turns these statuses into a verdict, so no single
component both observes and decides.
"""
from __future__ import annotations

from dataclasses import dataclass

EXACT = "EXACT"
TRIM_TRAILING_WS = "TRIM_TRAILING_WS"

PASSED = "PASSED"
WRONG_OUTPUT = "WRONG_OUTPUT"
RUNTIME_ERROR = "RUNTIME_ERROR"
TIMEOUT = "TIMEOUT"
MEMORY_EXCEEDED = "MEMORY_EXCEEDED"
OUTPUT_EXCEEDED = "OUTPUT_EXCEEDED"

_MEMORY_HINTS = ("MemoryError", "bad_alloc", "Cannot allocate memory", "out of memory")


def normalize(text: str, mode: str) -> str:
    if mode == EXACT:
        return text
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip("\n")


def outputs_match(actual: str, expected: str, mode: str) -> bool:
    return normalize(actual, mode) == normalize(expected, mode)


@dataclass(frozen=True)
class Observation:
    """What the runner saw. No judgement attached."""

    ordinal: int
    exit_code: int | None
    signal: int
    killed_by_timeout: bool
    time_ms: int
    cpu_ms: int | None
    memory_kb: int | None
    output: str
    output_bytes: int
    truncated: bool
    stderr_tail: str


def classify(
    observation: Observation,
    *,
    expected_output: str,
    compare_mode: str,
    time_limit_ms: int,
    memory_limit_mb: int,
) -> str:
    """Order matters: a limit breach outranks whatever the program managed to print."""
    if observation.killed_by_timeout:
        return TIMEOUT
    if observation.truncated:
        return OUTPUT_EXCEEDED
    # SIGXFSZ is the kernel enforcing the output limit we set as RLIMIT_FSIZE.
    if observation.signal == 25:
        return OUTPUT_EXCEEDED

    abnormal = observation.signal != 0 or (observation.exit_code or 0) != 0
    if abnormal:
        limit_kb = memory_limit_mb * 1024
        hit_memory = (observation.memory_kb or 0) >= int(limit_kb * 0.9) or any(
            hint in observation.stderr_tail for hint in _MEMORY_HINTS
        )
        if hit_memory:
            return MEMORY_EXCEEDED
        # A CPU-time kill (SIGKILL/SIGXCPU from RLIMIT_CPU) is a time limit, not a crash.
        if observation.signal in (9, 24) and observation.cpu_ms is not None:
            if observation.cpu_ms >= time_limit_ms:
                return TIMEOUT
        return RUNTIME_ERROR

    return PASSED if outputs_match(observation.output, expected_output, compare_mode) else WRONG_OUTPUT
