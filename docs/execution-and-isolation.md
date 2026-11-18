# Running submitted code, and the limits of this sandbox

One language is supported: `python3`. Adding another is three small changes, listed at the
bottom.

Every attempt records the mode it ran under (`judging_attempts.exec_mode`), every leaderboard
response carries `execution_mode`, and every test result file is labelled, so no measurement can
be mistaken for one taken under different conditions. Normally that value is `docker`. Attempts
completed by the benchmark dataset builder or by the protocol tests acting as the judge are
labelled `bench-double` or `test-double`: those wrote real verdicts through the real API, but
they executed nothing, and the label says so.

## What one attempt does

1. The worker leases the job over the judge plane and gets the source, the pinned limits, and
   the test inputs **and** expected outputs. The worker is trusted. The container is not.

2. It creates a container from the pinned runner image with:

   | Control | Value |
   | --- | --- |
   | network | `network_disabled=True`, so no interfaces at all |
   | user | `65534:65534`, never root |
   | root filesystem | read-only |
   | writable space | one tmpfs at `/box`, `exec,nosuid,nodev`, size-capped |
   | memory | `mem_limit = memswap_limit`, so no swap |
   | CPU | `nano_cpus`, one CPU by default |
   | processes | `pids_limit`, 64 by default |
   | capabilities | `cap_drop=["ALL"]` plus `no-new-privileges` |
   | host paths | none mounted, ever |
   | Docker socket | never passed in |

3. The job goes in on the container's **stdin**, not through the Docker archive API. That is
   what allows the read-only root filesystem: the daemon refuses an archive write into one, so
   the harness materialises the files into its tmpfs itself. Only inputs cross the boundary.
   Expected outputs stay in the worker.

4. The harness runs `py_compile` first, then for each test forks a child that applies limits
   before `exec`: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE` (the output limit), `RLIMIT_NPROC`,
   `RLIMIT_NOFILE`, `RLIMIT_CORE = 0`, and its own session via `setsid` so the whole process
   group can be killed at once.

5. Wall clock is enforced twice: the harness kills the process group past
   `time_limit_ms + 500ms`, and the worker gives up on the container past a budget derived from
   the limits. A container-level timeout is an *infrastructure* failure rather than a student
   verdict, because it means the harness itself misbehaved.

6. Output goes to a file whose size the kernel caps. Anything past the limit is reported as
   truncated, which becomes `OUTPUT_LIMIT_EXCEEDED`.

7. The harness reports observations only: exit status, signal, wall and CPU time, peak RSS,
   captured bytes. The worker compares outputs and maps the observation to a test status. The
   API derives the verdict. No single component both observes and decides.

8. The container is removed in a `finally` block, with its volumes. Every sandbox carries the
   label `shodh.judge=1`, and each worker sweeps strays with that label at start-up, so a worker
   killed mid-judge leaves nothing behind once it comes back.
   `tests/realrunner/test_isolation.py::TestCleanup` asserts zero leftovers after a run that
   includes a timeout and a crash.

## Limits are per problem version

`time_limit_ms`, `memory_limit_mb` and `output_limit_bytes` live on the problem version and get
copied into `judging_attempts.runtime_config` at submit time, along with the runner image tag.
So an old verdict can be explained exactly: the limits and the runner that produced it sit next
to it, rather than being looked up from today's configuration.

The runner image is stamped with a digest of the harness, and a worker rebuilds the image when
the digest does not match. Editing the harness cannot leave a stale sandbox judging submissions.

## What this sandbox does not protect against

Worth being explicit, because the brief asks:

* **It is Docker on a shared kernel, not a VM.** A kernel-level escape, a side channel between
  containers on the same host, or timing visibility through `/proc` are not addressed. A
  hostile-input contest would want gVisor, Firecracker, or something like nsjail underneath.
* **Seccomp is the Docker default profile**: not a judge-specific allowlist. Capabilities are
  all dropped and `no-new-privileges` is set, but a syscall allowlist would be tighter.
* **The worker holds the Docker socket.** That is the sibling-container pattern: the *worker*
  can talk to the daemon, which is equivalent to host root. Submitted code cannot. It runs in a
  container with no socket, no mounts and no network, and
  `test_the_docker_control_socket_is_not_reachable` asserts that on every run. A hardened
  deployment would put the workers on their own machines, or on a rootless or remote daemon, so
  that privilege is not sitting next to anything else.
* **No user-namespace remapping.** The sandbox runs as uid 65534 inside the container. With
  `userns-remap` on the daemon it would be unprivileged outside it too.
* **`/box` is mounted `exec`.** Submitted code has to be executable from somewhere. It is a
  size-capped tmpfs, `nosuid` and `nodev`, destroyed with the container.
* **Timings come from a busy host.** CPU time is bounded per process and wall clock twice, but
  on a machine also running PostgreSQL, two API processes and other sandboxes, a time limit is
  not a precise instrument. The seeded limits are generous (2 s, 128 MB) for that reason.
* **Memory classification is a heuristic.** An allocation refused by `RLIMIT_AS` usually
  produces `MemoryError`, which is reported as `MEMORY_LIMIT_EXCEEDED`. A kernel OOM kill that
  loses that signal is reported as `RUNTIME_ERROR`. Either way it is a student verdict with
  zero score, never a wrong answer and never a judge failure.

## Probes

The seed ships programs whose only job is to try something and fail, so the boundary is
asserted rather than described:

| Program | Asserts |
| --- | --- |
| `probe_network.py` | no network: a TCP connect fails |
| `probe_write_root.py` | the root filesystem is read-only |
| `probe_whoami.py` | `getuid() != 0` |
| `probe_docker_socket.py` | `/var/run/docker.sock` is absent |
| `probe_fork_bomb.py` | the process count is capped and the judge survives |
| `sum_two_timeout.py` | the wall clock limit is enforced |
| `sum_two_excessive_output.py` | the output limit is enforced and truncation is recorded |
| `sum_two_memory_hog.py` | the memory limit is enforced |

## Adding a language

1. Add `runner-images/<name>/Dockerfile` with the toolchain, copying `harness.py`.
2. Teach the harness how to compile and run it: two short branches in `_compile` and
   `_run_command`.
3. Add the enum value and image name in `api/app/runners.py`, and the env override in
   `docker-compose.yml`.

The worker builds a missing image on first use, so nothing else changes. A missing toolchain is
reported as an infrastructure failure rather than a compile error, because that is our problem
and not the author's.
