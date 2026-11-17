"""The two execution modes, and what each one actually guarantees.

docker  One sibling container per attempt, created through the daemon socket the *worker*
        holds. The container has no network, a read-only root filesystem, /box as a small
        tmpfs, a non-root user (65534), all capabilities dropped, no-new-privileges, and
        bounded memory, swap, CPU, process count, file size and wall clock. No host path is
        mounted into it and the Docker socket is never passed through, so submitted code
        cannot reach the control plane that started it. The container is removed in a finally
        block, and a reaper removes strays left by a worker that was killed.

local   The same harness, same limits, run as a subprocess of the worker. There is no
        container boundary: it exists so concurrency and recovery tests can run fast and
        deterministically, and every result it produces is labelled local. Only python3 is
        available in this mode - the worker image carries no C++ toolchain - and a language
        whose toolchain is missing is reported as an infrastructure failure, never as a
        compile error against the author.

Isolation limits worth stating plainly: this is Docker, not a microVM. A kernel escape, a
side channel between containers on the same host, or clock and /proc visibility are not
addressed. Seccomp is the Docker default profile; user namespaces are whatever the host
daemon is configured with. Those are the boundaries a reviewer should assume.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from .compare import Observation

log = logging.getLogger(__name__)

HARNESS_PATH = os.environ.get("HARNESS_PATH", "/srv/runner-images/harness.py")
RUNNER_CONTEXT = os.environ.get("RUNNER_CONTEXT", "/srv/runner-images")
SENTINEL = "###SHODH_RESULT###"
LABEL = "shodh.judge"

SOURCE_FILENAME = {"python3": "submission.py", "cpp17": "submission.cpp"}
DOCKERFILE_FOR = {"python3": "python/Dockerfile", "cpp17": "cpp/Dockerfile"}


class RunnerUnavailable(RuntimeError):
    """The judge could not judge. Never turned into a student verdict."""


def harness_digest() -> str:
    """Identity of the in-container harness, used to stamp and invalidate runner images."""
    import hashlib

    try:
        with open(HARNESS_PATH, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "unknown"


@dataclass
class JudgeOutcome:
    compile_ok: bool
    compile_message: str
    observations: list[Observation] = field(default_factory=list)
    exec_mode: str = "local"
    runner_image: str | None = None
    wall_ms: int = 0


def _manifest(job: dict) -> dict:
    config = job["runtime_config"]
    return {
        "language": job["language"],
        "time_limit_ms": config["time_limit_ms"],
        "memory_limit_mb": config["memory_limit_mb"],
        "output_limit_bytes": config["output_limit_bytes"],
        "nproc": config.get("pids_limit", 64),
        "failpoint": job.get("sandbox_failpoint"),
        "tests": [
            {"ordinal": case["ordinal"], "input_file": f"tests/{case['ordinal']}.in"}
            for case in job["test_cases"]
        ],
    }


def _job_blob(job: dict) -> bytes:
    """The job as the harness expects it on stdin: byte count, newline, base64 JSON.

    Only the program and the test *inputs* cross into the sandbox. Expected outputs stay in the
    worker, so nothing in the container has an answer key to find. Delivery over stdin rather
    than the Docker archive API is what lets the sandbox keep a read-only root filesystem.
    """
    language = job["language"]
    files = {SOURCE_FILENAME[language]: job["source_code"].encode("utf-8")}
    for case in job["test_cases"]:
        files[f"tests/{case['ordinal']}.in"] = case["input"].encode("utf-8")
    payload = base64.b64encode(
        json.dumps(
            {
                "manifest": _manifest(job),
                "files": {name: base64.b64encode(blob).decode("ascii") for name, blob in files.items()},
            }
        ).encode("utf-8")
    )
    return str(len(payload)).encode("ascii") + b"\n" + payload


def _parse_result(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(SENTINEL):
            return json.loads(line[len(SENTINEL) :])
    raise RunnerUnavailable("runner produced no parsable result")


def _observations(result: dict) -> list[Observation]:
    out = []
    for item in result.get("tests", []):
        out.append(
            Observation(
                ordinal=int(item["ordinal"]),
                exit_code=item.get("exit_code"),
                signal=int(item.get("signal") or 0),
                killed_by_timeout=bool(item.get("killed_by_timeout")),
                time_ms=int(item.get("time_ms") or 0),
                cpu_ms=item.get("cpu_ms"),
                memory_kb=item.get("memory_kb"),
                output=base64.b64decode(item.get("output_b64") or "").decode("utf-8", "replace"),
                output_bytes=int(item.get("output_bytes") or 0),
                truncated=bool(item.get("truncated")),
                stderr_tail=item.get("stderr_tail") or "",
            )
        )
    return out


def _outcome_from_result(result: dict, *, exec_mode: str, image: str | None, wall_ms: int) -> JudgeOutcome:
    if "harness_error" in result:
        raise RunnerUnavailable(f"harness failed: {result['harness_error']}")
    compile_info = result.get("compile") or {"ok": True, "message": ""}
    return JudgeOutcome(
        compile_ok=bool(compile_info.get("ok", True)),
        compile_message=str(compile_info.get("message") or "")[:4000],
        observations=_observations(result),
        exec_mode=exec_mode,
        runner_image=image,
        wall_ms=wall_ms,
    )


def _wall_budget_seconds(job: dict) -> float:
    config = job["runtime_config"]
    per_test = config["time_limit_ms"] / 1000.0 + 0.6
    return 25.0 + per_test * max(1, len(job["test_cases"]))


# --------------------------------------------------------------------------- local


class LocalRunner:
    mode = "local"

    def run(self, job: dict) -> JudgeOutcome:
        box = tempfile.mkdtemp(prefix="shodh-box-")
        started = time.monotonic()
        try:
            done = subprocess.run(
                ["python3", "-u", HARNESS_PATH],
                input=_job_blob(job).decode("ascii"),
                capture_output=True,
                text=True,
                timeout=_wall_budget_seconds(job),
                env=dict(os.environ, SHODH_BOX=box),
                cwd=box,
            )
            if done.returncode not in (0, 17, 18, 19, 20) and SENTINEL not in done.stdout:
                raise RunnerUnavailable(f"harness exited {done.returncode}: {done.stderr[-400:]}")
            result = _parse_result(done.stdout)
            return _outcome_from_result(
                result,
                exec_mode=self.mode,
                image=None,
                wall_ms=int((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerUnavailable("local runner exceeded its wall budget") from exc
        finally:
            shutil.rmtree(box, ignore_errors=True)


# --------------------------------------------------------------------------- docker


class DockerRunner:
    mode = "docker"

    def __init__(self) -> None:
        import docker  # imported lazily so the local mode needs no daemon at all

        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RunnerUnavailable(f"docker daemon unreachable: {exc}") from exc
        self._built: set[str] = set()

    # -- images ------------------------------------------------------------

    def ensure_image(self, language: str, image: str) -> None:
        """Build the runner image if it is missing or was built from a different harness.

        The harness digest is stamped on the image as a label, so editing the harness cannot
        leave a stale sandbox behind: the next judge run rebuilds, and every verdict can be
        traced to the runner that produced it.
        """
        if image in self._built:
            return
        import docker.errors

        digest = harness_digest()
        try:
            existing = self._client.images.get(image)
            if (existing.labels or {}).get("shodh.harness") == digest:
                self._built.add(image)
                return
            log.warning(
                "runner image was built from a different harness; rebuilding",
                extra={"image": image, "expected": digest, "found": (existing.labels or {}).get("shodh.harness")},
            )
        except docker.errors.ImageNotFound:
            log.info("building runner image", extra={"image": image, "language": language})
        self._client.images.build(
            path=RUNNER_CONTEXT,
            dockerfile=DOCKERFILE_FOR[language],
            tag=image,
            rm=True,
            pull=False,
            labels={"shodh.harness": digest},
        )
        self._built.add(image)

    def reap_strays(self) -> int:
        """Remove sandbox containers left behind by a worker that was killed mid-judge."""
        removed = 0
        try:
            for container in self._client.containers.list(all=True, filters={"label": LABEL}):
                try:
                    container.remove(force=True, v=True)
                    removed += 1
                except Exception:  # pragma: no cover
                    log.warning("could not remove stray sandbox", extra={"container": container.id[:12]})
        except Exception as exc:  # pragma: no cover
            log.warning("stray sweep failed", extra={"error": str(exc)})
        if removed:
            log.warning("removed stray sandbox containers", extra={"removed": removed})
        return removed

    # -- judging -----------------------------------------------------------

    def run(self, job: dict) -> JudgeOutcome:
        config = job["runtime_config"]
        language = job["language"]
        image = config.get("runner_image") or ""
        if language not in SOURCE_FILENAME:
            raise RunnerUnavailable(f"unsupported language {language}")
        self.ensure_image(language, image)

        memory_mb = int(config["memory_limit_mb"])
        tmpfs_mb = max(32, min(256, int(config["output_limit_bytes"] / (1024 * 1024)) + 48))
        name = f"shodh-judge-{job['attempt_id'][:8]}-{uuid.uuid4().hex[:6]}"
        started = time.monotonic()
        container = None
        stdin_socket = None
        try:
            container = self._client.containers.create(
                image=image,
                name=name,
                labels={LABEL: "1", "shodh.attempt": job["attempt_id"]},
                network_disabled=True,
                read_only=True,
                user="65534:65534",
                mem_limit=f"{memory_mb}m",
                memswap_limit=f"{memory_mb}m",
                nano_cpus=int(float(config.get("cpus", 1.0)) * 1_000_000_000),
                pids_limit=int(config.get("pids_limit", 64)),
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                # Docker mounts tmpfs noexec by default; a compiled submission has to be
                # executable from somewhere, and this size-capped tmpfs is the only writable
                # path in the container. nosuid and nodev stay on.
                tmpfs={"/box": f"rw,exec,nosuid,nodev,size={tmpfs_mb}m,mode=1777"},
                environment={"SHODH_BOX": "/box"},
                detach=True,
                stdin_open=True,
                tty=False,
            )
            container.start()
            # The harness is blocked reading stdin. Writing the job there (instead of copying
            # files in through the archive API) is what allows read_only=True on the rootfs.
            stdin_socket = container.attach_socket(params={"stdin": 1, "stream": 1})
            raw = getattr(stdin_socket, "_sock", stdin_socket)
            raw.sendall(_job_blob(job))
            try:
                raw.shutdown(1)  # SHUT_WR; not supported on every transport
            except OSError:
                pass
            # The socket stays open until the container has exited: closing it earlier can cut
            # the delivery short on some transports.
            exit_info = container.wait(timeout=_wall_budget_seconds(job))
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
            if SENTINEL not in stdout:
                raise RunnerUnavailable(
                    f"sandbox exited {exit_info.get('StatusCode')} without a result: {stderr[-400:]}"
                )
            return _outcome_from_result(
                _parse_result(stdout),
                exec_mode=self.mode,
                image=image,
                wall_ms=int((time.monotonic() - started) * 1000),
            )
        except RunnerUnavailable:
            raise
        except Exception as exc:
            raise RunnerUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if stdin_socket is not None:
                try:
                    stdin_socket.close()
                except Exception:  # pragma: no cover
                    pass
            if container is not None:
                try:
                    container.remove(force=True, v=True)
                except Exception:  # pragma: no cover
                    log.warning("sandbox cleanup failed", extra={"container": name})


def build_runner(mode: str):
    if mode == "docker":
        return DockerRunner()
    if mode == "local":
        return LocalRunner()
    raise RunnerUnavailable(f"unknown execution mode {mode}")
