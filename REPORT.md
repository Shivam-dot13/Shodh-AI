# Shodh a Code — engineering report

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 + Alembic · Docker.
`docker compose up --build` · `bash scripts/evaluate.sh` · `bash scripts/workload.sh`

## 1. Shape of the system

Three processes types, one database:

* **API** (two uvicorn worker processes by default) — contests, problems, submissions,
  standings, and a separate `/internal/judge/*` plane for the judge.
* **Judge workers** (two replicas, two attempts each) — pure HTTP clients of the judge plane
  plus a Docker driver. They hold no database credential at all, which makes the judge boundary
  a deployment fact rather than a convention.
* **PostgreSQL** — the schema, *and* the work queue.

The queue being a table is the central decision. `judge_jobs` is a transactional outbox and the
work queue at once: the queue row is inserted in the same transaction as the submission and its
first judging attempt. The window Stage 3 asks about — between committing a submission and
dispatching its job — therefore does not exist, because they are the same commit. Claiming is
`SELECT ... FOR UPDATE SKIP LOCKED`, so safety across workers is a property of PostgreSQL
rather than of my code, and scaling the judge is `--scale worker=N`. No broker, no cache, no
second datastore: I added nothing the problem did not need, and the one place a cache would
normally go (the leaderboard) is covered by a table written inside the verdict transaction, so
there is no refresh path to be stale.

What a submission does, end to end:

```
POST /contests/{id}/submissions          one transaction: submission + attempt 1 + queue row
   -> 201 with the identifier, status QUEUED            (judging has not started)
worker  POST /internal/judge/leases      FOR UPDATE SKIP LOCKED; lease_epoch += 1
        (sandbox container runs the program against the pinned tests)
        POST /internal/judge/attempts/{id}/result       guarded by state + lease_epoch
   -> one transaction: per-test rows, verdict, job DONE, submission status,
      standings cell recomputed, contests.standings_version += 1
GET /contests/{id}/leaderboard           reads the maintained cells; ETag = standings_version
```

## 2. Transaction boundaries and the invariants they protect

Full detail with the SQL is in [docs/transactions.md](docs/transactions.md). The summary:

| Write path             | Boundary                                                                | Key constraint                                        |
| ---------------------- | ----------------------------------------------------------------------- | ----------------------------------------------------- |
| create submission      | submission + attempt + queue row, one commit                             | `UNIQUE (contest_id, user_id, idempotency_key)`        |
| lease work             | claim + attempt transition, one commit                                   | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`           |
| store verdict          | per-test rows + attempt + job + submission + standings cell, one commit  | state guard + `lease_epoch` equality; `UNIQUE` per cell |
| infrastructure failure | attempt + job, one commit, **no score read or write**                    | `CHECK ((state <> 'INFRA_FAILED') OR (verdict IS NULL))` |
| reclaim lease          | job + attempt, one commit per batch, safe from every API process         | `FOR UPDATE SKIP LOCKED`; `lease_epoch += 1`           |
| rejudge                | lock submission, retire superseded jobs, append attempt, one commit      | `UNIQUE (submission_id, attempt_no)`                   |

Nothing holds a transaction open across a call to another service, and no mutual exclusion
lives inside a process — so the behaviour is identical with one API process or ten. Everything
runs at `READ COMMITTED`: the paths that need more take an explicit row lock or lean on a unique
index, which is why none of them needs `SERIALIZABLE` or a retry loop. Each connection sets
`statement_timeout = 15s` and `idle_in_transaction_session_timeout = 30s` so one stuck
statement cannot pin a worker or hold a lock indefinitely.

Three invariants are worth naming because the rest of the design falls out of them:

1. **One submission per (contest, actor, idempotency key).** Enforced by a unique index, so it
   survives restarts and any number of competing processes. Concurrent requests race to the
   insert; the loser catches the violation, re-reads the winner and returns it as a replay. A
   reused key with a different problem, language or source is `409 idempotency_key_conflict`,
   decided by a fingerprint hash rather than by comparing text.
2. **A verdict is written once, by the current delivery only.** The completion path locks the
   attempt, checks the state *and* the `lease_epoch`, and then writes. A second writer finds
   `COMPLETED`: at the same epoch that is an idempotent replay (the stored attempt comes back
   unchanged), at a different epoch it is `409 stale_judge_result`.
3. **Scoring is a recompute, never an increment.** The verdict transaction locks one
   `(contest, user, problem)` cell and recomputes it from that cell's authoritative attempts
   using the same pure functions a full rebuild uses. There is no `+=` anywhere in the scoring
   path, so a duplicated delivery, a replayed acknowledgement and an out-of-order completion all
   land in the same final state.

## 3. Retries, rejudges and ordering

**Guarantees, and where each one comes from.** Persistent storage gives atomic claiming,
one-job-per-attempt, the single-writer verdict and the one-row-per-cell score. The worker
protocol gives lease expiry, fencing by `lease_epoch`, a bounded delivery budget and
at-least-once delivery with an exactly-once effect. The runner gives only what a runner can:
whether a program that exceeded a limit is reported as a time limit rather than a crash, and
whether the sandbox actually contained it — which is why the execution mode is recorded on every
attempt and labelled in every result file.

**Fencing.** `lease_epoch` increments on every lease *and* on every reclaim. A worker that was
paused, partitioned or declared dead comes back to a refused result rather than a race. This is
asserted directly: a lease is taken, expired, reclaimed, re-leased by another worker, and the
original holder's result is rejected with `409` while the new one is accepted — one verdict, one
score, one set of per-test rows.

**Both failure windows.** After the submission commit there is nothing to lose, because the job
is in the same commit; the `crash_after_submission_commit` failpoint shows the client receiving a
`503` with no identifier while the database holds a submission, an attempt and a claimable job,
and the client's retry with the same key finds it judged. After judging but before the
acknowledgement, the lease expires, the work is reclaimed with a bumped epoch, the second
delivery reports, and the zombie's late result is refused.

**Rejudge and authority.** A rejudge appends an attempt against an explicitly chosen
`(problem_version, test_suite)` pair and retires the superseded job so it is not redelivered.
History is append-only. One rule decides what counts:

> the authoritative attempt of a submission is the `COMPLETED` attempt with the highest
> `attempt_no`.

An infrastructure failure never completes, so it can never be authoritative — a runner crash
cannot cost a participant a score they already earned. A late result from an attempt that a
rejudge replaced *is* stored, because throwing away a real measurement would be worse, but a
higher `attempt_no` has already completed, so the standings do not move.

**Out-of-order results.** The cell is a function of the stored set of attempts, not of arrival
order, so nothing special happens: an earlier submission that is rejected but finishes last
still contributes its penalty, and the solve still belongs to the submission that earned it.

**How clients see the leaderboard.** By polling, with `standings_version` — a counter
incremented inside the verdict transaction — as both the body field and the `ETag`;
`If-None-Match` answers `304`. There is no asynchronous refresh anywhere in the write path, so a
`200` is never behind a verdict that is already stored. "Consistent" is checked rather than
claimed: three independent implementations of the same answer (the maintained cells, a fresh
per-request derivation from `submissions`/`judging_attempts`, and a full rebuild in Python)
must agree byte for byte, in both scoring modes, after rejudges and after judge failures, and
`GET /contests/{id}/standings/verify` asserts it on demand.

## 4. Isolation choices

One sibling container per attempt, created through the socket the *worker* holds: no network, a
read-only root filesystem, a non-root user (65534), all capabilities dropped,
`no-new-privileges`, bounded memory with swap disabled, bounded CPU, `pids_limit`, a size-capped
tmpfs as the only writable path, no host mounts, and no Docker socket inside. Wall clock is
enforced twice (by the harness per test, by the worker for the whole container), output size by
`RLIMIT_FSIZE` plus a capped read, CPU time and address space by rlimits applied in the forked
child before `exec`.

Two choices are worth defending. First, the job is delivered on the container's **stdin** rather
than copied in with the Docker archive API — the daemon refuses an archive write into a
read-only rootfs, and I would rather keep the read-only rootfs than keep the convenience. The
harness materialises the files into its tmpfs itself. Second, **only inputs cross the
boundary**: expected outputs stay in the worker and comparison happens there, so even a program
that escaped its process has no answer key to find. The harness reports observations (exit
status, signal, wall and CPU time, peak RSS, captured bytes); the worker classifies them; the
API derives the verdict. No component both observes and decides.

The limits of this sandbox are stated plainly in
[docs/execution-and-isolation.md](docs/execution-and-isolation.md): Docker on a shared kernel
with the default seccomp profile, not a microVM; no user-namespace remapping; `/box` mounted
`exec` because a compiled submission has to be executable from somewhere; and the worker holding
the daemon socket, which is equivalent to host root and would belong on its own machine or a
rootless daemon in production. Submitted code cannot reach that socket, and a seeded probe
asserts it on every run. Cleanup is a `finally` block plus a labelled sweep at worker start-up,
so a worker killed mid-judge leaves nothing behind; the real-runner suite asserts zero leftover
containers after a run containing a timeout and a crash.
