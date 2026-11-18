"""Python submissions, executed for real in containers."""
from __future__ import annotations

import pytest

from conftest import submit, wait_for_terminal

pytestmark = pytest.mark.realrunner


@pytest.fixture(autouse=True)
def _needs_real_containers(execution_mode):
    if execution_mode != "docker":
        pytest.skip(f"execution mode is {execution_mode}, so real execution cannot be shown")


def judge(bundle, source):
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], source).json()
    return wait_for_terminal(rohan, submission["id"])["authoritative_attempt"]


def test_a_correct_program_is_accepted(fresh_contest, programs):
    attempt = judge(fresh_contest(), programs("accepted"))
    assert attempt["verdict"] == "ACCEPTED"
    assert attempt["tests_passed"] == attempt["tests_total"] == 3
    assert attempt["score"] == 100
    assert attempt["exec_mode"] == "docker"


def test_a_wrong_program_is_a_wrong_answer(fresh_contest, programs):
    attempt = judge(fresh_contest(), programs("wrong_output"))
    assert attempt["verdict"] == "WRONG_ANSWER"
    assert attempt["tests_passed"] == 0


def test_a_syntax_error_is_a_compile_error(fresh_contest, programs):
    attempt = judge(fresh_contest(), programs("compile_error"))
    assert attempt["verdict"] == "COMPILE_ERROR"
    assert attempt["score"] == 0
    assert attempt["tests_total"] == 0, "no test runs if the file will not compile"
    assert "submission.py" in attempt["compile_message"]


def test_version_lists_the_language_it_can_run(clients, manifest):
    token = manifest["organizations"]["acme-university"]["users"]["rohan@acme.test"]["token"]
    version = clients(token).get("/version").json()
    assert set(version["languages"]) == {"python3"}
    assert version["languages"]["python3"].startswith("shodh-runner-")
