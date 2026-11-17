"""Scenario family: execution. Real containers only.

Passing these against a test double would prove nothing, so every test here refuses to run
unless the system reports the docker execution mode. They submit probe programs through the
public API and assert what the sandbox did to them.

What this does *not* claim: these are Docker containers on a shared kernel, not microVMs. A
kernel exploit, a side channel between containers, or timing visibility through /proc are
outside the boundary. What is asserted is the boundary that was built: no network, no root, no
host mounts, no control socket, and enforced CPU, memory, process, wall-clock and output limits.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import submit, wait_for_terminal

pytestmark = pytest.mark.realrunner


@pytest.fixture(autouse=True)
def _needs_real_containers(execution_mode):
    if execution_mode != "docker":
        pytest.skip(f"execution mode is {execution_mode}; container isolation cannot be demonstrated")


def judge(bundle, programs, role, language="python3"):
    rohan = bundle["participants"]["rohan@acme.test"]
    submission = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs(role, language)
    ).json()
    return rohan, submission, wait_for_terminal(rohan, submission["id"])


class TestResourceLimits:
    def test_wall_clock_limit_is_enforced(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "timeout")
        attempt = final["authoritative_attempt"]
        assert attempt["verdict"] == "TIME_LIMIT_EXCEEDED"
        assert attempt["exec_mode"] == "docker"
        # The limit is 2000 ms and the runner killed it rather than letting it run on.
        assert attempt["time_ms"] is not None and attempt["time_ms"] < 10_000

    def test_output_limit_is_enforced(self, fresh_contest, programs, db):
        bundle = fresh_contest()
        rohan, submission, final = judge(bundle, programs, "excessive_output")
        assert final["authoritative_attempt"]["verdict"] == "OUTPUT_LIMIT_EXCEEDED"
        rows = db.execute(
            text(
                "SELECT r.output_bytes, r.truncated FROM attempt_test_results r"
                " WHERE r.attempt_id = :a ORDER BY r.ordinal"
            ),
            {"a": final["authoritative_attempt"]["id"]},
        ).mappings().all()
        assert rows, "per-test rows must exist"
        # 4 MiB was written; at most the 8 KiB limit was ever captured.
        assert all(row["output_bytes"] <= 8192 for row in rows)
        assert any(row["truncated"] for row in rows)

    def test_memory_limit_is_enforced(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "memory_exceeded")
        verdict = final["authoritative_attempt"]["verdict"]
        # An allocation past the limit shows up as a memory limit; a kernel OOM kill that loses
        # the allocation error is reported as a runtime error, never as a wrong answer.
        assert verdict in ("MEMORY_LIMIT_EXCEEDED", "RUNTIME_ERROR"), verdict
        assert final["authoritative_attempt"]["score"] == 0

    def test_a_crashing_program_is_a_runtime_error_not_a_judge_failure(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "runtime_failure")
        assert final["status"] == "JUDGED"
        assert final["authoritative_attempt"]["verdict"] == "RUNTIME_ERROR"

    def test_process_count_is_capped(self, fresh_contest, programs):
        """A fork bomb must be contained, and must not take the judge with it."""
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "probe_pids_limit")
        assert final["status"] == "JUDGED", "the judge survived and produced a verdict"
        assert final["authoritative_attempt"]["verdict"] in (
            "RUNTIME_ERROR",
            "TIME_LIMIT_EXCEEDED",
            "WRONG_ANSWER",
        )
        # The system is still judging afterwards.
        _, _, healthy = judge(bundle, programs, "accepted")
        assert healthy["authoritative_attempt"]["verdict"] == "ACCEPTED"


class TestSandboxBoundary:
    def test_the_sandbox_has_no_network(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "probe_network")
        # The probe exits non-zero when the connection fails, which is what we want to see.
        assert final["authoritative_attempt"]["verdict"] == "RUNTIME_ERROR"

    def test_the_root_filesystem_is_read_only(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "probe_readonly_rootfs")
        assert final["authoritative_attempt"]["verdict"] == "RUNTIME_ERROR"

    def test_submitted_code_does_not_run_as_root(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "probe_non_root")
        # exit 0 means getuid() was not 0; exit 6 would mean it ran as root.
        assert final["authoritative_attempt"]["verdict"] == "WRONG_ANSWER", "the probe ran and exited cleanly"
        assert final["authoritative_attempt"]["tests_passed"] == 0

    def test_the_docker_control_socket_is_not_reachable(self, fresh_contest, programs):
        bundle = fresh_contest()
        _, _, final = judge(bundle, programs, "probe_docker_socket")
        # exit 5 (runtime error) means the socket was absent. A clean exit would mean it was
        # present, which must never happen.
        assert final["authoritative_attempt"]["verdict"] == "RUNTIME_ERROR"


class TestCleanup:
    def test_no_sandbox_container_is_left_behind(self, fresh_contest, programs):
        """Every sandbox is labelled, so leftovers are countable rather than a matter of trust."""
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception as exc:
            pytest.skip(f"no docker daemon reachable from the test container: {exc}")

        bundle = fresh_contest()
        for role in ("accepted", "timeout", "runtime_failure"):
            judge(bundle, programs, role)

        leftovers = [
            f"{c.name} {c.status}"
            for c in client.containers.list(all=True, filters={"label": "shodh.judge"})
        ]
        assert leftovers == [], f"sandbox containers were not cleaned up: {leftovers}"

    def test_the_runner_image_is_recorded_on_every_attempt(self, fresh_contest, programs, db):
        bundle = fresh_contest()
        _, submission, final = judge(bundle, programs, "accepted")
        row = db.execute(
            text("SELECT runner_image, exec_mode, runtime_config FROM judging_attempts WHERE id = :a"),
            {"a": final["authoritative_attempt"]["id"]},
        ).mappings().one()
        assert row["exec_mode"] == "docker"
        assert row["runner_image"] == row["runtime_config"]["runner_image"]
        assert row["runner_image"].startswith("shodh-runner-")
