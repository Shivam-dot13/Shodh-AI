"""The documented attempt state model, asserted rather than described."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/srv/api")

from app.domain.enums import (  # noqa: E402
    ATTEMPT_TRANSITIONS,
    AttemptState,
    TERMINAL_ATTEMPT_STATES,
    Verdict,
    can_transition,
)
from app.domain.failpoints import source_failpoint  # noqa: E402

pytestmark = pytest.mark.unit


def test_every_state_is_declared():
    assert set(ATTEMPT_TRANSITIONS) == set(AttemptState)


def test_terminal_states_have_no_exit():
    for state in TERMINAL_ATTEMPT_STATES:
        assert ATTEMPT_TRANSITIONS[state] == set(), state


def test_queue_to_verdict_is_the_happy_path():
    assert can_transition(AttemptState.PENDING, AttemptState.DISPATCHED)
    assert can_transition(AttemptState.DISPATCHED, AttemptState.RUNNING)
    assert can_transition(AttemptState.RUNNING, AttemptState.COMPLETED)


def test_work_can_return_to_the_queue_but_not_from_a_verdict():
    assert can_transition(AttemptState.RUNNING, AttemptState.PENDING)
    assert can_transition(AttemptState.DISPATCHED, AttemptState.PENDING)
    assert not can_transition(AttemptState.COMPLETED, AttemptState.PENDING)
    assert not can_transition(AttemptState.COMPLETED, AttemptState.RUNNING)


def test_a_judge_failure_is_not_a_verdict_state():
    assert AttemptState.INFRA_FAILED in TERMINAL_ATTEMPT_STATES
    assert not can_transition(AttemptState.INFRA_FAILED, AttemptState.COMPLETED)
    assert "INFRA_FAILED" not in {v.value for v in Verdict}


def test_pending_cannot_jump_straight_to_completed():
    """A verdict requires a lease, which is what makes the fencing token meaningful."""
    assert not can_transition(AttemptState.PENDING, AttemptState.COMPLETED)


class TestSourceFailpointMarker:
    def test_marker_is_read_from_a_comment(self):
        assert source_failpoint("# SHODH_FAILPOINT:runner_unavailable\nprint(1)") == "runner_unavailable"

    def test_marker_in_a_c_comment_is_read_too(self):
        assert source_failpoint("// SHODH_FAILPOINT:runner_crash_in_container") == "runner_crash_in_container"

    def test_absent_marker_returns_nothing(self):
        assert source_failpoint("print('hello')") is None

    def test_marker_far_down_the_file_is_ignored(self):
        source = "\n" * 50 + "# SHODH_FAILPOINT:runner_unavailable"
        assert source_failpoint(source) is None
