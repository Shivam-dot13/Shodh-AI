"""Verdict derivation and output comparison, in isolation."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/srv/api")
sys.path.insert(0, "/srv")

from app.domain.enums import TestStatus as Status, Verdict  # noqa: E402
from app.services.verdicts import TestOutcome as Outcome, decide  # noqa: E402
from worker import compare  # noqa: E402

pytestmark = pytest.mark.unit


def outcomes(*statuses: Status, weight: int = 1) -> list[Outcome]:
    return [Outcome(ordinal=i, status=s, weight=weight) for i, s in enumerate(statuses, start=1)]


class TestVerdictMapping:
    def test_all_passed_is_accepted_with_full_points(self):
        result = decide(compiled=True, outcomes=outcomes(Status.PASSED, Status.PASSED), max_points=100)
        assert (result.verdict, result.score, result.tests_passed) == (Verdict.ACCEPTED, 100, 2)

    def test_compile_failure_short_circuits_everything(self):
        result = decide(compiled=False, outcomes=outcomes(Status.PASSED), max_points=100)
        assert (result.verdict, result.score) == (Verdict.COMPILE_ERROR, 0)

    @pytest.mark.parametrize(
        "status,expected",
        [
            (Status.WRONG_OUTPUT, Verdict.WRONG_ANSWER),
            (Status.RUNTIME_ERROR, Verdict.RUNTIME_ERROR),
            (Status.TIMEOUT, Verdict.TIME_LIMIT_EXCEEDED),
            (Status.MEMORY_EXCEEDED, Verdict.MEMORY_LIMIT_EXCEEDED),
            (Status.OUTPUT_EXCEEDED, Verdict.OUTPUT_LIMIT_EXCEEDED),
        ],
    )
    def test_first_failure_by_ordinal_names_the_verdict(self, status, expected):
        result = decide(
            compiled=True,
            outcomes=outcomes(Status.PASSED, status, Status.WRONG_OUTPUT),
            max_points=100,
        )
        assert result.verdict is expected

    def test_ordinal_order_not_arrival_order_decides(self):
        reported = [
            Outcome(ordinal=2, status=Status.TIMEOUT),
            Outcome(ordinal=1, status=Status.WRONG_OUTPUT),
        ]
        assert decide(compiled=True, outcomes=reported, max_points=100).verdict is Verdict.WRONG_ANSWER

    def test_partial_credit_is_weighted_and_never_reaches_full_marks(self):
        result = decide(
            compiled=True,
            outcomes=outcomes(Status.PASSED, Status.PASSED, Status.PASSED, Status.WRONG_OUTPUT),
            max_points=100,
        )
        assert result.score == 75 and result.verdict is Verdict.WRONG_ANSWER

        nearly = decide(
            compiled=True,
            outcomes=outcomes(*([Status.PASSED] * 999 + [Status.WRONG_OUTPUT])),
            max_points=100,
        )
        assert nearly.score == 99  # floor, and capped below max_points

    def test_weights_are_respected(self):
        reported = [
            Outcome(ordinal=1, status=Status.PASSED, weight=3),
            Outcome(ordinal=2, status=Status.WRONG_OUTPUT, weight=1),
        ]
        assert decide(compiled=True, outcomes=reported, max_points=100).score == 75

    def test_empty_suite_is_not_a_free_pass(self):
        result = decide(compiled=True, outcomes=[], max_points=100)
        assert (result.verdict, result.score) == (Verdict.WRONG_ANSWER, 0)


class TestComparison:
    def test_trailing_whitespace_mode_ignores_line_and_file_endings(self):
        assert compare.outputs_match("5\n", "5", compare.TRIM_TRAILING_WS)
        assert compare.outputs_match("5  \n\n", "5\n", compare.TRIM_TRAILING_WS)
        assert compare.outputs_match("a\r\nb\r\n", "a\nb\n", compare.TRIM_TRAILING_WS)

    def test_trailing_whitespace_mode_still_rejects_different_content(self):
        assert not compare.outputs_match("5", "6", compare.TRIM_TRAILING_WS)
        assert not compare.outputs_match(" 5", "5", compare.TRIM_TRAILING_WS)  # leading space matters

    def test_exact_mode_is_byte_for_byte(self):
        assert compare.outputs_match("5\n", "5\n", compare.EXACT)
        assert not compare.outputs_match("5\n", "5", compare.EXACT)


def observation(**kwargs) -> compare.Observation:
    base = dict(
        ordinal=1,
        exit_code=0,
        signal=0,
        killed_by_timeout=False,
        time_ms=10,
        cpu_ms=5,
        memory_kb=1000,
        output="5\n",
        output_bytes=2,
        truncated=False,
        stderr_tail="",
    )
    base.update(kwargs)
    return compare.Observation(**base)


class TestClassification:
    def _classify(self, obs, expected="5\n", time_limit_ms=1000, memory_limit_mb=128):
        return compare.classify(
            obs,
            expected_output=expected,
            compare_mode=compare.TRIM_TRAILING_WS,
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
        )

    def test_clean_match_passes(self):
        assert self._classify(observation()) == compare.PASSED

    def test_mismatch_is_wrong_output(self):
        assert self._classify(observation(output="6\n")) == compare.WRONG_OUTPUT

    def test_timeout_outranks_a_correct_partial_answer(self):
        assert self._classify(observation(killed_by_timeout=True)) == compare.TIMEOUT

    def test_truncated_output_is_an_output_limit(self):
        assert self._classify(observation(truncated=True)) == compare.OUTPUT_EXCEEDED

    def test_sigxfsz_is_an_output_limit(self):
        assert self._classify(observation(signal=25, exit_code=None)) == compare.OUTPUT_EXCEEDED

    def test_nonzero_exit_is_a_runtime_error(self):
        assert self._classify(observation(exit_code=1, output="")) == compare.RUNTIME_ERROR

    def test_allocation_failure_is_a_memory_limit(self):
        obs = observation(exit_code=1, output="", stderr_tail="MemoryError")
        assert self._classify(obs) == compare.MEMORY_EXCEEDED

    def test_peak_rss_near_the_limit_is_a_memory_limit(self):
        obs = observation(exit_code=137, output="", memory_kb=120 * 1024)
        assert self._classify(obs, memory_limit_mb=128) == compare.MEMORY_EXCEEDED

    def test_cpu_kill_is_reported_as_a_time_limit_not_a_crash(self):
        obs = observation(exit_code=-9, signal=9, output="", cpu_ms=1500, memory_kb=1000)
        assert self._classify(obs, time_limit_ms=1000) == compare.TIMEOUT
