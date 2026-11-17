#!/usr/bin/env python3
"""In-container judging harness. Trusted code, baked into the runner image.

It is deliberately dumb: it compiles, runs each test once under per-process limits, and
reports *observations* (exit status, signal, wall time, CPU time, peak RSS, captured bytes).
It never sees an expected output - comparison happens in the worker - so even a program that
escaped its process could not read an answer key out of this container.

Job delivery
    The job arrives on stdin as a decimal byte count, a newline, then that many bytes of
    base64-encoded JSON: {"manifest": {...}, "files": {"<name>": "<base64>"}}. Nothing is
    copied in through the Docker archive API, which means the sandbox can run with a read-only
    root filesystem and /box as its only writable mount.

Output
    One line on stdout: "###SHODH_RESULT###" followed by compact JSON.

Expected environment: unprivileged user, no network, read-only root filesystem, /box a small
tmpfs. The local execution mode points SHODH_BOX at a temporary directory instead and runs the
identical code with no container around it.
"""
from __future__ import annotations

import base64
import json
import os
import resource
import signal
import subprocess
import sys
import time

BOX = os.environ.get("SHODH_BOX", "/box")
RESULT = os.path.join(BOX, "result.json")
POLL_SECONDS = 0.004
STDIN_TIMEOUT_SECONDS = 30.0
SENTINEL = "###SHODH_RESULT###"


def _emit(payload: dict) -> None:
    """Results travel out on stdout; the file is a convenience for manual inspection."""
    encoded = json.dumps(payload)
    try:
        with open(RESULT, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    except OSError:
        pass
    sys.stdout.write(SENTINEL + encoded + "\n")
    sys.stdout.flush()


def _read_job() -> dict:
    header = sys.stdin.readline()
    if not header.strip().isdigit():
        raise ValueError("job header must be a decimal byte count")
    remaining = int(header.strip())
    chunks: list[str] = []
    read = 0
    while read < remaining:
        chunk = sys.stdin.read(remaining - read)
        if not chunk:
            raise ValueError(f"job truncated: expected {remaining} bytes, got {read}")
        chunks.append(chunk)
        read += len(chunk)
    return json.loads(base64.b64decode("".join(chunks)))


def _materialize(files: dict[str, str]) -> None:
    for name, blob in files.items():
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"illegal job file name {name!r}")
        path = os.path.join(BOX, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(blob))


def _compile(language: str, time_budget_s: float) -> dict:
    """Python gets a syntax check, C++ a real compile. Either can fail the attempt early."""
    started = time.monotonic()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPYCACHEPREFIX": os.path.join(BOX, "pycache"),
        "HOME": BOX,
        "TMPDIR": BOX,
    }
    if language == "python3":
        command = [sys.executable, "-m", "py_compile", "submission.py"]
    elif language == "cpp17":
        command = ["g++", "-O2", "-std=c++17", "-static", "-w", "-o", "prog", "submission.cpp"]
    else:
        return {"ok": False, "message": f"unsupported language {language}", "time_ms": 0}
    try:
        done = subprocess.run(
            command, cwd=BOX, capture_output=True, text=True, timeout=time_budget_s, env=env
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "compilation timed out", "time_ms": int(time_budget_s * 1000)}
    except FileNotFoundError as exc:
        return {"ok": False, "message": f"compiler missing: {exc}", "time_ms": 0}
    return {
        "ok": done.returncode == 0,
        "message": (done.stderr or done.stdout or "")[-4000:],
        "time_ms": int((time.monotonic() - started) * 1000),
    }


def _run_command(language: str) -> list[str]:
    if language == "python3":
        return [sys.executable, "-I", "-S", os.path.join(BOX, "submission.py")]
    return [os.path.join(BOX, "prog")]


def _child(command: list[str], input_path: str, output_path: str, limits: dict) -> None:
    """Executes in the forked child: apply limits, redirect stdio, exec the submitted program."""
    try:
        os.setsid()
        cpu_seconds = max(1, int(limits["time_limit_ms"] / 1000) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        mem_bytes = limits["memory_limit_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        fsize = limits["output_limit_bytes"] + 4096
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits["nproc"], limits["nproc"]))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        stdin_fd = os.open(input_path, os.O_RDONLY)
        stdout_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        stderr_fd = os.open(output_path + ".err", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(stdin_fd, 0)
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.chdir(BOX)
        os.execvpe(
            command[0],
            command,
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": BOX,
                "TMPDIR": BOX,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    except BaseException as exc:  # pragma: no cover - the child must never return to Python
        # Say why the program could not be started; otherwise a setup failure in here is
        # indistinguishable from a program that crashed on its own.
        try:
            with open(output_path + ".err", "ab") as handle:
                handle.write(f"harness_child_error: {type(exc).__name__}: {exc}\n".encode())
        except Exception:
            pass
        os._exit(127)


def _run_one(command: list[str], ordinal: int, input_path: str, limits: dict) -> dict:
    output_path = os.path.join(BOX, f"out_{ordinal}.txt")
    wall_limit = limits["time_limit_ms"] / 1000.0 + 0.5
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        _child(command, input_path, output_path, limits)

    killed_by_timeout = False
    status = 0
    usage = None
    while True:
        done_pid, status, usage = os.wait4(pid, os.WNOHANG)
        if done_pid != 0:
            break
        if time.monotonic() - started > wall_limit:
            killed_by_timeout = True
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            done_pid, status, usage = os.wait4(pid, 0)
            break
        time.sleep(POLL_SECONDS)
    wall_ms = int((time.monotonic() - started) * 1000)

    exit_code = os.waitstatus_to_exitcode(status)
    term_signal = -exit_code if exit_code < 0 else 0

    limit = limits["output_limit_bytes"]
    truncated = False
    captured = b""
    try:
        size = os.path.getsize(output_path)
        with open(output_path, "rb") as handle:
            captured = handle.read(limit + 1)
        if len(captured) > limit or size > limit:
            truncated = True
            captured = captured[:limit]
    except FileNotFoundError:
        pass
    stderr_tail = ""
    try:
        with open(output_path + ".err", "rb") as handle:
            stderr_tail = handle.read(2048).decode("utf-8", "replace")
    except FileNotFoundError:
        pass
    for path in (output_path, output_path + ".err"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    return {
        "ordinal": ordinal,
        "exit_code": exit_code if exit_code >= 0 else None,
        "signal": term_signal,
        "killed_by_timeout": killed_by_timeout,
        "time_ms": wall_ms,
        "cpu_ms": int((usage.ru_utime + usage.ru_stime) * 1000) if usage else None,
        "memory_kb": int(usage.ru_maxrss) if usage else None,
        "output_bytes": len(captured),
        "truncated": truncated,
        "output_b64": base64.b64encode(captured).decode("ascii"),
        "stderr_tail": stderr_tail[-512:],
    }


def main() -> int:
    job = _read_job()
    manifest = job["manifest"]
    _materialize(job.get("files") or {})

    # A submitted program may ask the runner to fail on purpose, so recovery paths are testable
    # without waiting for a real crash. Honoured only inside the sandbox.
    if manifest.get("failpoint") == "runner_crash_in_container":
        _emit({"harness_error": "runner crashed on purpose (failpoint)"})
        return 17

    language = manifest["language"]
    limits = {
        "time_limit_ms": int(manifest["time_limit_ms"]),
        "memory_limit_mb": int(manifest["memory_limit_mb"]),
        "output_limit_bytes": int(manifest["output_limit_bytes"]),
        "nproc": int(manifest.get("nproc", 64)),
    }
    compile_result = _compile(language, min(20.0, 10.0 + limits["time_limit_ms"] / 1000))
    if not compile_result["ok"]:
        _emit({"compile": compile_result, "tests": []})
        return 0

    command = _run_command(language)
    results = [
        _run_one(command, int(test["ordinal"]), os.path.join(BOX, test["input_file"]), limits)
        for test in manifest["tests"]
    ]
    _emit({"compile": compile_result, "tests": results})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover
        try:
            _emit({"harness_error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        sys.exit(18)
