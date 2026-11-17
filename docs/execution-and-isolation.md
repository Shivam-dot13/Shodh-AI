# Executing submitted code, and the limits of this sandbox

## The two execution modes

Set with `EXEC_MODE`. Every attempt records the mode it was judged under
(`judging_attempts.exec_mode`), every leaderboard response carries `execution_mode`, and every
test result file is labelled, so no measurement can be mistaken for one taken under a different
mode.

| Mode     | Boundary                                   | What it is for                                       |
| -------- | ------------------------------------------ | ---------------------------------------------------- |
| `docker` | one locked-down sibling container per attempt | the default; the only mode that demonstrates isolation |
| `local`  | a subprocess of the worker, rlimits only   | fast, deterministic runs with no container boundary (python3 only) |

Both modes run the **same** harness (`runner-images/harness.py`) and the same comparison code.
`local` differs only in that nothing contains the program except the process limits, which is
exactly why its results are labelled and why `tests/realrunner/` refuses to run unless the
system reports `docker`. It can run python3 only: the worker image has no C++ toolchain, and a
language whose compiler is absent from a runner is reported as an infrastructure failure rather
than as a compile error, because a missing toolchain is our problem and not the author's.

A third label, `bench-double` / `test-double`, appears on attempts completed by the benchmark
dataset builder or by the protocol tests acting as the judge. Those write real verdicts through
the real API, but they never executed anything, and the label says so.

## What one attempt does, in `docker` mode

1. The worker leases the job over the judge plane and receives the source, the pinned limits
   and the test **inputs and expected outputs**. The worker is trusted; the container is not.
2. It creates a container from the pinned runner image with:

   | Control                  | Value                                      |
   | ------------------------ | ------------------------------------------ |
   | network                  | `network_disabled=True` — no interfaces     |
   | user                     | `65534:65534`, never root                   |
   | root filesystem          | read-only                                   |
   | writable space           | one tmpfs at `/box`, `exec,nosuid,nodev`, size-capped |
   | memory                   | `mem_limit = memswap_limit` (no swap)       |
   | CPU                      | `nano_cpus` (1 CPU by default)              |
   | processes                | `pids_limit` (64 by default)                |
   | capabilities             | `cap_drop=["ALL"]`, `no-new-privileges`     |
   | host paths               | none mounted, ever                          |
   | Docker socket            | never passed in                             |

3. The job is written to the container's **stdin**, not copied in with the archive API. That is
   what allows the read-only root filesystem: the harness materialises the files into `/box`
   itself. Only inputs cross the boundary; expected outputs stay in the worker.
4. The harness compiles (`py_compile` or `g++ -O2 -std=c++17 -static`), then for each test
   forks a child that applies per-process limits before `exec`:

   `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE` (the output limit), `RLIMIT_NPROC`,
   `RLIMIT_NOFILE`, `RLIMIT_CORE = 0`, its own session via `setsid` so a whole process group
   can be killed.

5. Wall clock is enforced twice: the harness kills the process group past
   `time_limit_ms + 500ms`, and the worker gives up on the whole container past a budget derived
   from the limits. A container-level timeout is an *infrastructure* failure, not a student
   verdict, because it means the harness itself misbehaved.
6. Output is captured to a file whose size the kernel caps; more than the limit is reported as
   truncated, which becomes `OUTPUT_LIMIT_EXCEEDED`.
7. The harness reports **observations only** — exit status, signal, wall and CPU time, peak RSS,
   captured bytes. The worker compares outputs and maps the observation to a test status; the
   API derives the verdict. No single component both observes and decides.
8. The container is removed in a `finally` block, with its volumes. Every sandbox carries the
   label `shodh.judge=1`, and each worker sweeps strays with that label at start-up — so a
   worker killed mid-judge leaves nothing behind after it comes back.
   `tests/realrunner/test_isolation.py::TestCleanup` asserts zero leftovers after a run that
   includes a timeout and a crash.

## Resource limits are per problem version

`time_limit_ms`, `memory_limit_mb` and `output_limit_bytes` live on the problem version and are
copied into `judging_attempts.runtime_config` at submit time, together with the runner image
tag. A historical verdict can therefore be explained exactly: the limits and the runner that
produced it are stored next to it, not looked up from today's configuration.

The runner image is stamped with a digest of the harness, and a worker rebuilds the image when
the digest does not match — so editing the harness cannot leave a stale sandbox judging
submissions.

## Isolation limits of this environment — stated plainly

* **It is Docker on a shared kernel, not a VM.** A kernel-level escape, a side channel between
  containers on the same host, or timing visibility through `/proc` are not addressed. A
  hostile-input contest would want gVisor, Firecracker or an unprivileged sandbox like
  nsjail/isolate underneath.
* **Seccomp is the Docker default profile.** It is not a judge-specific allowlist. All
  capabilities are dropped and `no-new-privileges` is set, but a syscall-level allowlist would
  be tighter.
* **The worker holds the Docker socket.** That is the sibling-container pattern: the *worker*
  can talk to the daemon, which is equivalent to host root. Submitted code cannot — it runs in a
  container with no socket, no mounts and no network, and
  `tests/realrunner/test_isolation.py::test_the_docker_control_socket_is_not_reachable` asserts
  it. A hardened deployment would put the workers on their own machines, or on a rootless or
  remote daemon, so that privilege is not co-located with anything else.
* **No user namespace remapping.** The sandbox runs as uid 65534 inside the container; with
  `userns-remap` on the daemon it would also be unprivileged outside it.
* **`/box` is mounted `exec`.** A compiled submission has to be executable from somewhere.
  It is a size-capped tmpfs, `nosuid` and `nodev`, and it is destroyed with the container.
* **Timings are measured on a busy host.** CPU time is bounded per process and wall clock
  twice, but on a machine that is also running PostgreSQL, two API processes and other
  sandboxes, a time limit is not a precise instrument. The limits are generous
  (2 s, 128 MB) relative to the seeded problems for that reason.
* **Memory classification is a heuristic.** An allocation refused by `RLIMIT_AS` usually
  produces `MemoryError`/`bad_alloc`, which is reported as `MEMORY_LIMIT_EXCEEDED`; a kernel OOM
  kill that loses that signal is reported as `RUNTIME_ERROR`. Either way it is a student verdict
  with zero score, never a wrong answer and never a judge failure.

## Isolation probes

The seed bundle ships programs whose only job is to try something and fail, so the boundary is
asserted rather than described:

| Program                   | Asserts                                             |
| ------------------------- | --------------------------------------------------- |
| `probe_network.py`        | no network: a TCP connect fails                     |
| `probe_write_root.py`     | the root filesystem is read-only                    |
| `probe_whoami.py`         | `getuid() != 0`                                     |
| `probe_docker_socket.py`  | `/var/run/docker.sock` is absent                    |
| `probe_fork_bomb.py`      | the process count is capped and the judge survives  |
| `sum_two_timeout.py`      | the wall clock limit is enforced                    |
| `sum_two_excessive_output.py` | the output limit is enforced and truncation is recorded |
| `sum_two_memory_hog.py`   | the memory limit is enforced                        |

## Adding a language

1. Add a `runner-images/<name>/Dockerfile` that installs the toolchain and copies
   `harness.py`.
2. Teach the harness how to compile and run it (two short branches).
3. Add the enum value and the image name in `api/app/runners.py`, and the env override in
   `docker-compose.yml`.

The worker builds a missing image on first use, so nothing else has to change.
