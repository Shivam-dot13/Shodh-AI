# Shodh a Code — engineering report

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 + Alembic · Docker
`docker compose up --build` · `bash scripts/evaluate.sh` · `bash scripts/workload.sh`

## 1. Architecture

Two API processes, two judge workers, one database. Participants and organizers use the public
API with a `user` credential; the workers use `/internal/judge/*` with a `judge` credential and
hold no database credential at all, which makes the judge boundary a deployment fact rather
than a convention.

```
POST /contests/{id}/submissions   one transaction: submission + attempt 1 + queue row
                                  → 201 with the identifier, status QUEUED
worker POST /internal/judge/leases   FOR UPDATE SKIP LOCKED, lease_epoch += 1
       ... sandbox container runs the program against the pinned tests ...
       POST .../attempts/{id}/result one transaction: per-test rows, verdict, job DONE,
                                     submission status, cell recomputed, version += 1
GET /contests/{id}/leaderboard    maintained cells; ETag = standings_version
```

**The queue is a table, not a broker.** `judge_jobs` is a transactional outbox and the work
queue at once: its row is inserted in the same transaction as the submission and the first
attempt, so the window between "submission committed" and "job dispatched" does not exist —
they are the same commit. Claiming is `SELECT ... FOR UPDATE SKIP LOCKED`, so safety across
workers belongs to PostgreSQL rather than to my code, and scaling the judge is
`--scale worker=N`. Nothing was added that the problem did not need: the one place a cache would
normally sit, the leaderboard, is a table written inside the verdict transaction, so there is no
refresh path that can be stale.

## 2. Transaction boundaries and the invariants they protect

| Write path        | Boundary                                                            | Enforced by                                            |
| ----------------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| create submission | submission + attempt + queue row, one commit                        | `UNIQUE (contest_id, user_id, idempotency_key)`         |
| lease work        | claim + attempt transition, one commit                              | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`            |
| store verdict     | per-test rows, attempt, job, submission, standings cell, one commit | state guard + `lease_epoch` equality; one row per cell   |
| judge failure     | attempt + job, one commit, **no score read or write**               | `CHECK ((state <> 'INFRA_FAILED') OR (verdict IS NULL))` |
| reclaim lease     | job + attempt, one batch, safe from every API process               | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`            |
| rejudge           | lock submission, retire superseded job, append attempt, one commit  | `UNIQUE (submission_id, attempt_no)`                    |

No transaction is held open across a call to another service and no mutual exclusion lives
inside a process, so behaviour is identical with one API process or ten. Everything runs at
`READ COMMITTED`; the paths needing more take a row lock or lean on a unique index, so none
needs `SERIALIZABLE` or a retry loop.

Three invariants carry the design.

1. **One submission per (contest, actor, key)** — a unique index, so it holds across restarts
   and processes. Concurrent requests race the insert; the loser catches the violation, re-reads
   the winner and answers as a replay. A key reused with different code, problem or language is
   `409 idempotency_key_conflict`, decided by a fingerprint hash.
2. **A verdict is written once, by the current delivery only** — the completion path locks the
   attempt and checks the state *and* the `lease_epoch`. A second writer at the same epoch gets
   the stored attempt back unchanged (a safe retry); at a different epoch, `409`.
3. **Scoring is a recompute, never an increment** — the verdict transaction locks one
   `(contest, user, problem)` cell and recomputes it from that cell's authoritative attempts
   using the same pure functions a full rebuild uses. There is no `+=` in the scoring path, so a
   duplicated delivery, a replayed acknowledgement and an out-of-order completion all converge.

## 3. Retries, rejudging and ordering

**Where each guarantee comes from.** *Storage:* atomic claiming, one job per attempt, the
single-writer verdict, one row per cell. *Worker protocol:* lease expiry, fencing by
`lease_epoch`, a bounded delivery budget, at-least-once delivery with an exactly-once effect.
*Runner:* only what a runner can give — whether a program that broke a limit is reported as a
time limit rather than a crash, and whether the sandbox really contained it. Hence the execution
mode is stored on every attempt and labelled in every result file.

**Fencing.** `lease_epoch` increments on every lease *and* every reclaim, so a worker that was
paused, partitioned or declared dead returns to a refused result instead of a race. Asserted
directly: lease, expire, reclaim, re-lease elsewhere, and the original holder is rejected with
`409` while the new delivery is accepted — one verdict, one score, one set of per-test rows.
That also closes the second failure window (judged, then dead before acknowledging); the first
one, a crash after the submission commit, has nothing to lose because the job is in that commit.
Section 7 shows both with their log trails.

**Authority.** A rejudge appends an attempt against an explicitly chosen
`(problem_version, test_suite)` pair — per submission or for a whole problem — and retires the
superseded job. History is append-only, and one rule decides what counts:

> the authoritative attempt of a submission is the `COMPLETED` attempt with the highest
> `attempt_no`.

An infrastructure failure never completes, so it is never authoritative: a runner crash cannot
cost a participant a score they earned. A late result from a replaced attempt *is* stored —
discarding a real measurement would be worse — but a higher `attempt_no` has completed, so the
standings do not move. Out-of-order results need no special case: the cell is a function of the
stored set, not of arrival order.

**How clients observe the leaderboard.** By polling `standings_version` — incremented inside the
verdict transaction, returned in the body and as the `ETag`, with `If-None-Match` answering
`304`. No asynchronous refresh exists in the write path, so a `200` is never behind a stored
verdict. Consistency is checked, not claimed: three independent implementations of the same
answer (maintained cells, a per-request derivation, a full rebuild in Python) must agree byte for
byte after rejudges and after judge failures.

## 4. Isolation

One sibling container per attempt, created through the socket the *worker* holds: no network,
read-only root filesystem, non-root user, all capabilities dropped, `no-new-privileges`, bounded
memory with swap disabled, bounded CPU, `pids_limit`, a size-capped tmpfs as the only writable
path, no host mounts, no Docker socket inside. Wall clock is enforced twice (harness per test,
worker per container); output size, CPU time and address space by rlimits applied in the forked
child before `exec`.

Two choices worth defending. The job is delivered on the container's **stdin** rather than
through the Docker archive API, because the daemon refuses an archive write into a read-only
rootfs and I would rather keep the read-only rootfs; the harness materialises the files into its
own tmpfs. And **only inputs cross the boundary** — expected outputs stay in the worker and
comparison happens there, so a program that escaped its process still has no answer key to find.
The harness reports observations, the worker classifies them, the API derives the verdict: no
component both observes and decides.

The limits are in [docs/execution-and-isolation.md](docs/execution-and-isolation.md): Docker on a
shared kernel with the default seccomp profile, not a microVM; no user-namespace remapping;
`/box` mounted `exec` because a compiled submission must be executable somewhere; and the worker
holding the daemon socket, which is host-root equivalent and belongs on its own machine in
production. Submitted code cannot reach that socket, and a seeded probe asserts it on every run.
Cleanup is a `finally` block plus a labelled sweep at worker start-up, and the real-runner suite
asserts zero leftover containers after a run containing a timeout and a crash.

## 5. Performance evidence

One developer machine: Docker Desktop on WSL2, 8 vCPU and 7.7 GiB visible, with PostgreSQL,
both API processes, both judge workers and the load generator sharing it. Absolute values belong
to that machine as much as to the code; the comparisons are between runs on the same host,
minutes apart. Every figure is in `evidence/workload-*.json` and `evidence/bottleneck-*.json`,
labelled with commit, execution mode, dataset and request mix.

**Dataset** (built through the public API, history judged by a synthetic judge labelled
`bench-double`, then reused byte-for-byte by both runs): 120 participants, 5 problems, 1,800
judged submissions, 600 cells, ICPC. **Workload:** 32 virtual users, closed loop, 60 s, mix
30 % submit / 10 % submission detail / 10 % submission list / 50 % leaderboard, mode `docker`.

| | uncontended p50 | 32 users p50 | p95 | p99 |
| ------------------------------- | ---: | ---: | ---: | ---: |
| `GET /healthz`                  | 1.5 ms  | — | — | — |
| `GET /leaderboard`              | 12.4 ms | 128.3 ms | 469.7 ms | 623.0 ms |
| `GET /contests/{id}/submissions`| 10.0 ms | 225.4 ms | 434.8 ms | 521.5 ms |
| `GET /submissions/{id}`         | —       | 190.4 ms | 414.0 ms | 489.3 ms |
| `POST /submissions`             | 14.7 ms | 207.5 ms | 685.3 ms | 780.8 ms |

*API only — judge stopped, nothing executed: 125.5 req/s, 0 errors in 7,553 requests.* Same mix
with the judge running: **58.4 req/s, 0 errors in 3,529**, leaderboard p50 458.1 ms, submit p50
669.4 ms.

Uncontended the API costs 10–15 ms per request, and `/healthz` at 1.5 ms shows that is database
work, not framework overhead. At 32 users the deployment sits at its throughput ceiling, so the
percentiles are mostly queueing: Little's law puts the average at 32 ÷ 125 ≈ 255 ms, which is
about where the p50s land. Running the judge costs 53 % of throughput (125 → 58 req/s) because
four sandboxes compete for the same eight cores.

**API overhead versus program execution.** Judging is asynchronous, so execution is never inside
a response; it comes from the stored records:

| | p50 | p95 |
| ------------------------------------------------- | ---: | ---: |
| program execution (what the submitted code spent) | 47 ms | 84 ms |
| judge wall time per attempt (lifecycle included)  | 1,853 ms | 3,076 ms |
| queue delay (job committed → leased)              | 164 s | 265 s |

The middle row is the finding worth acting on: **97 % of judge wall time is container lifecycle,
not the program.** Four sandboxes at ~1.85 s is a shade over 2 attempts/s while the API accepts
17 submissions/s, so under sustained load the queue grows and the 164 s delay is that arithmetic,
not a defect. An independent run confirms the rate: draining a 1,632-job backlog produced 2,256
verdicts in ~12 minutes, 3.1 a second. The queue is the shock absorber and capacity scales with `--scale worker=N`; the
change with the best return is a pre-warmed sandbox pool instead of create/start/wait/remove per
attempt, and it touches one class.

## 6. The investigated bottleneck: the leaderboard

Every participant polls it. Two implementations of the same answer are kept in the code so the
difference can be measured: `naive` derives every cell per request from `submissions` and
`judging_attempts` (`DISTINCT ON` for each submission's authoritative attempt, then window
functions for the ICPC rules); `standings` reads the `problem_scores` cells the verdict
transaction already maintained. Measured sequentially, 60 requests per path, judge stopped:

| 400 participants × 6 problems  | judged submissions | naive DB time | maintained DB time | client p50 | p95 |
| ------------------------------ | ---: | ---: | ---: | --- | --- |
| `bottleneck-leaderboard`       | 2,400 | 16.7 ms | **3.1 ms** | 29.4 → 18.6 ms | 1.6× |
| `bottleneck-leaderboard-large` | 9,600 | 58.9 ms | **2.9 ms** | 54.9 → 18.6 ms | 3.4× |

`EXPLAIN (ANALYZE, BUFFERS)` says why: the naive path sequentially scans `judging_attempts`,
hash-joins it to the contest's submissions, then sorts and window-aggregates the result, while
the maintained path scans 2,400 narrow `problem_scores` rows once and hash-aggregates them.
Quadrupling the submissions makes the naive path **3.5× slower** while the maintained path does
not move at all — the point is not the 5–20× ratio at any one size but that naive cost grows
with stored attempts while maintained cost grows with participants. The client sees a smaller
multiple (1.6× then 3.4× at p95) because a fixed ~10 ms of HTTP, auth, serialisation and
the per-problem cell query is in every request; the database time is what decides whether a
50,000-submission contest stays usable.

**Trade-off.** The maintained cells cost one locked-row update per verdict, and
`standings_version` serialises verdict commits within a contest — nowhere near binding when
judging is bounded at ~2 attempts/s. **Correctness after the change** is checked:
`serving == rebuild` and `serving == naive` hold at both sizes, and recomputing all 2,400 cells
from the authoritative records changes nothing (`rebuild_changed_anything: false`).

## 7. One injected failure, its recovery, and the stored outcome

`tests/integration/test_recovery.py` covers both windows, asserting the stored records and not
just the HTTP responses. Both are also followable in the logs by request id, across the API and
the judge.

**A — crash after the submission commit.** With `crash_after_submission_commit` armed on every
API process the client gets `503` and never learns an identifier, yet the database holds the
submission, its attempt and a `READY` queue row. A worker picks the job up on its next poll; the
client's retry with the same key returns `200 replayed=true` on a submission that is now
`ACCEPTED`, score 100. One request id crosses `submission accepted → failpoint tripped → request
rejected → judging started → verdict stored`.

**B — a runner that refuses to run.** A seeded program carrying
`SHODH_FAILPOINT:runner_unavailable` is delivered three times, at `lease_epoch` 1, 2 and 3 with
exponential backoff, then the budget stops it: job `DEAD`, attempt `INFRA_FAILED`,
`verdict = null`, score 0, submission `JUDGE_FAILED`, leaderboard byte-identical before and
after, consistency check still passing.

## Known limitations

1. **Isolation is Docker, not a VM** — shared kernel, default seccomp, no user-namespace
   remapping, `/box` mounted `exec`; hostile input would want gVisor, Firecracker or nsjail. The
   worker also holds the Docker socket (host-root equivalent): submitted code cannot reach it,
   but in production the judge belongs on its own machines or a rootless daemon.
2. **Container lifecycle dominates judging** (1.85 s against 47 ms of program); a pre-warmed
   pool is the next piece of work.
3. **`standings_version` is a hot row** serialising verdict commits per contest — it buys a
   freshness marker with no stale-cache path; a per-contest sequence would relax it.
4. **The leaderboard is polled, not pushed** — cheap with `ETag`, but no SSE or WebSocket feed.
5. **Authentication is a fixture** — static tokens, no expiry or rotation, confined to one
   module. There is also no admission control beyond `max_attempts_per_problem`: no rate limit,
   judge quota or fair-share scheduling, so one participant can fill the queue.
6. **No contest freeze**, and no streaming progress for a very large rejudge.
7. **One language, python3.** A missing toolchain is reported as an infrastructure failure
   rather than a compile error. Benchmark history is written by a labelled synthetic judge, so a
   dataset's past verdicts did not execute code; each attempt records the mode that made it.
