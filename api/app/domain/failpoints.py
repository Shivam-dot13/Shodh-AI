"""Deterministic, explicitly armed fault injection.

Recovery is testable without waiting for a random crash. A failpoint is armed either by the
SHODH_FAILPOINTS environment variable (process-local, set at start-up) or at runtime through
POST /dev/failpoints, which records it in the database so that *every* API process honours it -
the deployment runs more than one, and a fault that only half the fleet obeys proves nothing.

Runtime arming is refused unless the development authentication fixture is enabled.

A submitted program can also carry a marker (SHODH_FAILPOINT:<name>) that makes the runner fail
on purpose; the worker honours only the two names that cannot be abused, and only while the
fixture is enabled.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .errors import InfrastructureError

log = logging.getLogger(__name__)

# API-side failpoints.
CRASH_AFTER_SUBMISSION_COMMIT = "crash_after_submission_commit"
CRASH_BEFORE_SCORE_APPLY = "crash_before_score_apply"
SLOW_LEADERBOARD = "slow_leaderboard"

# Worker-side failpoints (read by the worker process from its own environment).
CRASH_BEFORE_RESULT_ACK = "crash_before_result_ack"
RUNNER_UNAVAILABLE = "runner_unavailable"

# Marker understood inside submitted source code.
SOURCE_MARKER = "SHODH_FAILPOINT:"

KNOWN = {
    CRASH_AFTER_SUBMISSION_COMMIT,
    CRASH_BEFORE_SCORE_APPLY,
    SLOW_LEADERBOARD,
    CRASH_BEFORE_RESULT_ACK,
    RUNNER_UNAVAILABLE,
    "runner_crash_in_container",
}

REFRESH_SECONDS = 0.25

_lock = threading.Lock()
_env_armed: set[str] = set()
_shared_armed: set[str] = set()
_loader: Callable[[], set[str]] | None = None
_last_refresh = 0.0


def load(initial: set[str]) -> None:
    """Arm the process-local set from the environment."""
    with _lock:
        _env_armed.clear()
        _env_armed.update(initial)


def set_loader(loader: Callable[[], set[str]] | None) -> None:
    """Install the shared-state reader. Called once at start-up."""
    global _loader, _last_refresh
    with _lock:
        _loader = loader
        _last_refresh = 0.0


def _refresh(force: bool = False) -> None:
    global _last_refresh
    with _lock:
        loader = _loader
        due = force or (time.monotonic() - _last_refresh) >= REFRESH_SECONDS
    if loader is None or not due:
        return
    try:
        names = loader()
    except Exception:  # pragma: no cover - never let diagnostics break a request
        log.debug("failpoint refresh failed", exc_info=True)
        return
    with _lock:
        _shared_armed.clear()
        _shared_armed.update(names)
        _last_refresh = time.monotonic()


def invalidate() -> None:
    """Force the next lookup to re-read the shared state."""
    global _last_refresh
    with _lock:
        _last_refresh = 0.0


def armed() -> set[str]:
    _refresh()
    with _lock:
        return set(_env_armed | _shared_armed)


def active(name: str) -> bool:
    with _lock:
        if name in _env_armed:
            return True
    _refresh()
    with _lock:
        return name in _shared_armed


def trip(name: str, message: str | None = None) -> None:
    """Raise InfrastructureError if the named failpoint is armed."""
    if active(name):
        log.warning("failpoint tripped", extra={"failpoint": name})
        raise InfrastructureError(message or f"failpoint {name} tripped", code=f"failpoint_{name}")


def source_failpoint(source_code: str) -> str | None:
    """Return the failpoint a submitted program asks the runner to trip, if any."""
    for line in source_code.splitlines()[:20]:
        index = line.find(SOURCE_MARKER)
        if index >= 0:
            return line[index + len(SOURCE_MARKER) :].strip().strip("*/#").strip() or None
    return None
