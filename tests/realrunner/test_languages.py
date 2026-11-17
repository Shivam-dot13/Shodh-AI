"""Both documented languages, executed for real."""
from __future__ import annotations

import pytest

from conftest import submit, wait_for_terminal

pytestmark = pytest.mark.realrunner


@pytest.fixture(autouse=True)
def _needs_real_containers(execution_mode):
    if execution_mode != "docker":
        pytest.skip(f"execution mode is {execution_mode}; real compilation cannot be demonstrated")


@pytest.mark.parametrize("language", ["python3", "cpp17"])
def test_a_correct_program_is_accepted(fresh_contest, programs, language):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("accepted", language),
        language=language,
    ).json()
    final = wait_for_terminal(rohan, submission["id"])
    attempt = final["authoritative_attempt"]
    assert attempt["verdict"] == "ACCEPTED", f"{language}: {attempt}"
    assert attempt["tests_passed"] == attempt["tests_total"] == 3
    assert attempt["exec_mode"] == "docker"


@pytest.mark.parametrize("language", ["python3", "cpp17"])
def test_a_wrong_program_is_a_wrong_answer(fresh_contest, programs, language):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("wrong_output", language),
        language=language,
    ).json()
    final = wait_for_terminal(rohan, submission["id"])
    assert final["authoritative_attempt"]["verdict"] == "WRONG_ANSWER"


@pytest.mark.parametrize("language", ["python3", "cpp17"])
def test_a_broken_program_is_a_compile_error(fresh_contest, programs, language):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("compile_error", language),
        language=language,
    ).json()
    final = wait_for_terminal(rohan, submission["id"])
    attempt = final["authoritative_attempt"]
    assert attempt["verdict"] == "COMPILE_ERROR"
    assert attempt["score"] == 0
    assert attempt["tests_total"] == 0, "no test is run when compilation fails"
    assert attempt["compile_message"], "the compiler output is returned to the author"
    # The message is the compiler talking about the submitted file, not about our internals.
    assert "submission" in attempt["compile_message"]


def test_a_cpp_runtime_failure_is_a_runtime_error(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan,
        bundle["contest_id"],
        bundle["problem_id"],
        programs("runtime_failure", "cpp17"),
        language="cpp17",
    ).json()
    final = wait_for_terminal(rohan, submission["id"])
    assert final["authoritative_attempt"]["verdict"] == "RUNTIME_ERROR"


def test_the_version_endpoint_lists_the_languages_it_can_run(clients, manifest):
    client = clients(manifest["organizations"]["acme-university"]["users"]["rohan@acme.test"]["token"])
    version = client.get("/version").json()
    assert set(version["languages"]) == {"python3", "cpp17"}
    assert all(image.startswith("shodh-runner-") for image in version["languages"].values())
