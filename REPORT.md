# Shodh a Code — engineering report

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 + Alembic · Docker
`docker compose up --build` · `bash scripts/evaluate.sh` · `bash scripts/workload.sh`

## 1. Architecture

Two API processes, two judge workers, one database. Participants and organizers use the
public API with a `user` credential; the workers use `/internal/judge/*` with a `judge`
credential and hold no database credential at all, which makes the judge boundary a deployment
fact rather than a convention.

```
POST /contests/{id}/submissions   ─┐ one transaction: submission + attempt 1 + queue row
                                   └► 201 with the identifier, status QUEUED
worker  POST /internal/judge/leases    FOR UPDATE SKIP LOCKED, lease_epoch += 1
        ... sandbox container runs the program against the pinned tests ...
        POST /internal/judge/attempts/{id}/result
                                   ─┐ one transaction: per-test rows, verdict, job DONE,
                                     │ submission status, standings cell recomputed,
                                   └► contests.standings_version += 1
GET /contests/{id}/leaderboard         maintained cells; ETag = standings_version
```

**The queue is a table, not a broker.** `judge_jobs` is a transactional outbox and the work
queue at once: its row is inserted in the same transaction as the submission and the first
attempt. The window between "submission committed" and "job dispatched" therefore does not
exist — they are the same commit. Claiming is `SELECT ... FOR UPDATE SKIP LOCKED`, so safety
across workers belongs to PostgreSQL rather than to my code, and scaling the judge is
`--scale worker=N`. I added no broker, no cache and no second datastore: the one place a cache
would normally sit (the leaderboard) is a table written inside the verdict transaction, so
there is no refresh path that can be stale.

## 2. Transaction boundaries and the invariants they protect

Full SQL in [docs/transactions.md](docs/transactions.md).

| Write path             | Boundary                                                              | Enforced by                                             |
| ---------------------- | --------------------------------------------------------------------- | ------------------------------------------------------- |
| create submission      | submission + attempt + queue row, one commit                          | `UNIQUE (contest_id, user_id, idempotency_key)`          |
| lease work             | claim + attempt transition, one commit                                | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`             |
| store verdict          | per-test rows, attempt, job, submission, standings cell — one commit   | state guard + `lease_epoch` equality; one row per cell    |
| judge failure          | attempt + job, one commit, **no score read or write**                 | `CHECK ((state <> 'INFRA_FAILED') OR (verdict IS NULL))`  |
| reclaim lease          | job + attempt, one batch, safe from every API process                 | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`             |
| rejudge                | lock submission, retire superseded job, append attempt, one commit    | `UNIQUE (submission_id, attempt_no)`                     |

No transaction is held open across a call to another service, and no mutual exclusion lives
inside a process, so behaviour is identical with one API process or ten. Everything runs at
`READ COMMITTED`; the paths that need more take an explicit row lock or lean on a unique index,
which is why none needs `SERIALIZABLE` or a retry loop. Each connection sets
`statement_timeout = 15s` and `idle_in_transaction_session_timeout = 30s`.

Three invariants carry the design:

1. **One submission per (contest, actor, key)** — a unique index, so it survives restarts and
   any number of processes. Concurrent requests race the insert; the loser catches the violation,
   re-reads the winner and answers as a replay. A key reused with different code, problem or
   language is `409 idempotency_key_conflict`, decided by a fingerprint hash.
2. **A verdict is written once, by the current delivery only** — the completion path locks the
   attempt and checks both the state and the `lease_epoch`. A second writer at the same epoch
   gets the stored attempt back unchanged (a safe retry); at a different epoch it gets
   `409 stale_judge_result`.
3. **Scoring is a recompute, never an increment** — the verdict transaction locks one
   `(contest, user, problem)` cell and recomputes it from that cell's authoritative attempts with
   the same pure functions a full rebuild uses. There is no `+=` in the scoring path, so a
   duplicated delivery, a replayed acknowledgement and an out-of-order completion all converge
   on the same state.

## 3. Retries, rejudging and ordering

**Where each guarantee comes from.** *Storage:* atomic claiming, one job per attempt, the
single-writer verdict, one row per score cell. *Worker protocol:* lease expiry, fencing by
`lease_epoch`, a bounded delivery budget, at-least-once delivery with an exactly-once effect.
*Runner:* only what a runner can give — whether a program that exceeded a limit is reported as a
time limit rather than a crash, and whether the sandbox really contained it. That is why the
execution mode is stored on every attempt and labelled in every result file.

**Fencing.** `lease_epoch` increments on every lease *and* every reclaim, so a worker that was
paused, partitioned or declared dead returns to a refused result instead of a race. Asserted
directly: lease, expire, reclaim, re-lease elsewhere, and the original holder's result is
rejected with `409` while the new one is accepted — one verdict, one score, one set of per-test
rows.

**Both failure windows.** After the submission commit there is nothing to lose, because the job
is in that commit; the `crash_after_submission_commit` failpoint shows the client getting a
`503` with no identifier while the database holds a submission, an attempt and a claimable job,
and the client's retry with the same key finds it judged. After judging but before the
acknowledgement, the lease expires, the work is reclaimed with a bumped epoch, the second
delivery reports, and the zombie's late result is refused.

**Authority.** A rejudge appends an attempt against an explicitly chosen
`(problem_version, test_suite)` pair — per submission, or for every submission of a problem —
and retires the superseded job so it is not redelivered. History is append-only, and one rule
decides what counts:

> the authoritative attempt of a submission is the `COMPLETED` attempt with the highest
> `attempt_no`.

An infrastructure failure never completes, so it is never authoritative: a runner crash cannot
cost a participant a score they earned. A late result from a replaced attempt *is* stored —
discarding a real measurement would be worse — but a higher `attempt_no` has already completed,
so the standings do not move. Results completing out of order need no special case, because the
cell is a function of the stored set rather than of arrival order.

**How clients observe the leaderboard.** By polling, with `standings_version` — incremented
inside the verdict transaction — as both a body field and the `ETag`; `If-None-Match` answers
`304`. There is no asynchronous refresh in the write path, so a `200` is never behind a verdict
that is already stored. Consistency is checked rather than claimed: three independent
implementations of the same answer (maintained cells, a fresh per-request derivation from
`submissions`/`judging_attempts`, and a full rebuild in Python) must agree byte for byte in both
scoring modes, after rejudges and after judge failures.

## 4. Isolation

One sibling container per attempt, created through the socket the *worker* holds: no network,
read-only root filesystem, non-root user (65534), all capabilities dropped,
`no-new-privileges`, bounded memory with swap disabled, bounded CPU, `pids_limit`, a size-capped
tmpfs as the only writable path, no host mounts, and no Docker socket inside. Wall clock is
enforced twice (by the harness per test, by the worker for the container), output size by
`RLIMIT_FSIZE` plus a capped read, CPU time and address space by rlimits applied in the forked
child before `exec`.

Two choices worth defending. The job is delivered on the container's **stdin** rather than
copied in through the Docker archive API, because the daemon refuses an archive write into a
read-only rootfs and I would rather keep the read-only rootfs; the harness materialises the
files into its tmpfs itself. And **only inputs cross the boundary** — expected outputs stay in
the worker and comparison happens there, so a program that escaped its process still has no
answer key to find. The harness reports observations (exit status, signal, wall and CPU time,
peak RSS, captured bytes), the worker classifies them, the API derives the verdict: no component
both observes and decides.

The limits are stated plainly in
[docs/execution-and-isolation.md](docs/execution-and-isolation.md) — Docker on a shared kernel
with the default seccomp profile, not a microVM; no user-namespace remapping; `/box` mounted
`exec` because a compiled submission must be executable from somewhere; and the worker holding
the daemon socket, which is equivalent to host root and belongs on its own machine in
production. Submitted code cannot reach that socket, and a seeded probe asserts it on every run.
Cleanup is a `finally` block plus a labelled sweep at worker start-up, and the real-runner suite
asserts zero leftover containers after a run containing a timeout and a crash.
