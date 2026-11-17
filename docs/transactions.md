# Transaction boundaries, and the constraints that protect each invariant

Each write path below is one transaction. Nothing in the system holds a transaction open across
a network call to another service, and nothing relies on a lock that lives inside one process —
every mutual exclusion is a database row lock, so the behaviour is the same with one API
process or ten.

## 1. Create a submission

**Boundary:** one transaction containing the submission, its first judging attempt, and the
queue row.

```
INSERT submissions          (contest, user, problem, language, source, idempotency_key, fingerprint)
INSERT judging_attempts     (attempt_no = 1, pinned problem_version + test_suite + runtime_config)
INSERT judge_jobs           (attempt_id, state = READY)
COMMIT
```

| Invariant                                      | Protected by                                                   |
| ---------------------------------------------- | -------------------------------------------------------------- |
| one submission per (contest, actor, key)        | `UNIQUE (contest_id, user_id, idempotency_key)`                 |
| a committed submission always has a job         | same transaction — no window exists to lose                     |
| a retried dispatch cannot duplicate work        | `UNIQUE (judge_jobs.attempt_id)`                                |
| a verdict's meaning cannot drift                | `runtime_config` + `problem_version_id` + `test_suite_id` pinned on the attempt |

Concurrent requests with the same key race to the `INSERT`. One wins; the loser catches the
unique violation, re-reads the winner and returns it as a replay. The response to the loser is
indistinguishable from a sequential replay, which is the point.

A reused key with a *different* request is a conflict, not a replay: `request_fingerprint` is a
hash of the problem, language and source, and a mismatch answers
`409 idempotency_key_conflict`.

The outbox is why the Stage 3 dispatch window does not exist. A crash anywhere after this
commit leaves a durable, claimable job — the `crash_after_submission_commit` failpoint
demonstrates exactly that, and the client's retry with the same key finds the submission
already judged.

## 2. Lease work

**Boundary:** one transaction per claim batch.

```
WITH candidate AS (
    SELECT id FROM judge_jobs
    WHERE state = 'READY' AND available_at <= now()
    ORDER BY priority, available_at, id
    FOR UPDATE SKIP LOCKED LIMIT :capacity        -- atomic claim across all workers
)
UPDATE judge_jobs SET state='LEASED', leased_by=…, lease_expires_at=now()+…,
                      delivery_count = delivery_count + 1
UPDATE judging_attempts SET state='DISPATCHED', lease_epoch = lease_epoch + 1
  WHERE id = … AND state IN ('PENDING','DISPATCHED','RUNNING')     -- guarded transition
COMMIT
```

`SKIP LOCKED` is what makes two workers claiming at the same instant safe: the row lock is held
in PostgreSQL, so correctness does not depend on how many worker or API processes exist. The
guarded `UPDATE` means a job whose attempt was superseded by a rejudge is quietly retired
instead of judged twice.

`lease_epoch` increments on every lease. It is the fencing token the rest of the protocol rests
on.

## 3. Store a verdict

**Boundary:** one transaction containing the per-test rows, the attempt, the queue row, the
submission status and the standings cell. Either all of it is visible or none of it is.

```
SELECT … FROM judging_attempts WHERE id = :id FOR UPDATE
  -- refuse unless lease_epoch matches and the state is DISPATCHED or RUNNING
INSERT attempt_test_results …
UPDATE judging_attempts SET state='COMPLETED', verdict, score, …
UPDATE judge_jobs        SET state='DONE'
UPDATE submissions       SET status, authoritative_attempt_id          -- re-derived
SELECT … FROM problem_scores WHERE (contest,user,problem) FOR UPDATE   -- insert-if-absent
UPDATE problem_scores    SET points, solved, penalty_minutes, …        -- recomputed
UPDATE contests          SET standings_version = standings_version + 1
COMMIT
```

| Invariant                                        | Protected by                                                        |
| ------------------------------------------------ | ------------------------------------------------------------------- |
| at most one verdict per attempt                   | `FOR UPDATE` + a state guard; a second writer sees `COMPLETED`       |
| only the current delivery may write               | `lease_epoch` equality check                                        |
| a completed attempt always has a verdict          | `CHECK ((state <> 'COMPLETED') OR (verdict IS NOT NULL))`            |
| a judge failure never has one                     | `CHECK ((state <> 'INFRA_FAILED') OR (verdict IS NULL))`             |
| a score cannot exceed the problem's maximum       | `CHECK (score >= 0 AND score <= max_score)`                          |
| points cannot be awarded twice                    | one row per cell (`UNIQUE (contest,user,problem)`), recomputed not incremented |
| per-test rows cannot be duplicated                | `UNIQUE (attempt_id, test_case_id)`                                  |

Replaying the same acknowledgement at the same `lease_epoch` returns the stored attempt and
writes nothing, so a worker that crashed between judging and acknowledging can safely retry. A
*different* epoch is refused with `409 stale_judge_result`.

Two workers finishing two submissions of the same cell serialise on the `problem_scores` row.
The loser recomputes from a set that already includes the winner, so the result is the same in
either order.

## 4. Report an infrastructure failure

**Boundary:** one transaction.

Within the delivery budget the attempt returns to `PENDING`, the job returns to `READY` with
`available_at = now() + min(30s, 2^delivery_count)`. On the last delivery the attempt becomes
`INFRA_FAILED` with `verdict = NULL` and the job becomes `DEAD`. Neither path touches a score —
the standings cell is not even read, so a runner crash cannot lower a score that an earlier
attempt earned.

## 5. Reclaim an abandoned lease

**Boundary:** one transaction per batch, run by a reaper in every API process every two
seconds (and on demand through `POST /internal/judge/reap`).

```
WITH expired AS (
    SELECT id, attempt_id FROM judge_jobs
    WHERE state='LEASED' AND lease_expires_at < now() AND delivery_count < max_deliveries
    FOR UPDATE SKIP LOCKED LIMIT :batch
), requeued AS (UPDATE judge_jobs SET state='READY', … RETURNING attempt_id)
UPDATE judging_attempts SET state='PENDING', lease_epoch = lease_epoch + 1 FROM requeued …
```

`SKIP LOCKED` makes it safe for every process to run this at once. Incrementing `lease_epoch`
on reclaim is deliberate: the worker that vanished is fenced out, so if it comes back to life
mid-judge its result is refused rather than racing the new delivery.

A second statement handles jobs whose budget is spent: `DEAD`, and the attempt
`INFRA_FAILED`.

## 6. Rejudge

**Boundary:** one transaction.

```
SELECT … FROM submissions WHERE id = :id FOR UPDATE      -- serialise concurrent rejudges
SELECT max(attempt_no) …                                  -- next attempt number
UPDATE judge_jobs SET state='DONE' …                      -- stop redelivery of superseded work
INSERT judging_attempts (attempt_no = max + 1, chosen problem_version + test_suite)
INSERT judge_jobs
INSERT audit_log
COMMIT
```

Earlier attempts are untouched: the history is append-only and
`UNIQUE (submission_id, attempt_no)` keeps it that way. An in-flight older attempt is not
cancelled — if it reports, its result is recorded as history, but a higher `attempt_no` has
already completed, so the standings do not move.

## 7. Publish a problem version

**Boundary:** one transaction creating the suite, its cases and the version, and optionally
pointing `problems.current_version_id` at it.

Versions and suites are immutable once created. New data means a new version, never an edit, so
a historical verdict keeps its meaning. `problem_versions.default_test_suite_id` and
`judging_attempts.problem_version_id` / `test_suite_id` are `ON DELETE RESTRICT`: a suite that
a verdict depends on cannot be deleted out from under it.

## Isolation level and why it is enough

Everything runs at PostgreSQL's default `READ COMMITTED`. The paths that need more than that
take explicit row locks (`FOR UPDATE`) or rely on a unique constraint to serialise them, which
is why no path needs `SERIALIZABLE` or a retry-on-serialization-failure loop:

* idempotent creation → unique index, and the loser of the race reads the winner;
* verdict application → `FOR UPDATE` on the attempt, plus an epoch and state guard;
* scoring → `FOR UPDATE` on the cell, then a recompute rather than a read-modify-write;
* claiming → `FOR UPDATE SKIP LOCKED`.

Operationally, each connection sets `statement_timeout = 15s` and
`idle_in_transaction_session_timeout = 30s`, so one stuck statement cannot pin an API worker or
hold a lock indefinitely.

## Hot row, named

`contests.standings_version` is incremented in every verdict transaction, which serialises
verdict commits *within one contest*. That is a real limit and it is a deliberate trade: it
gives clients a monotonic marker to poll (and an `ETag`) with no asynchronous refresh path
anywhere, so a leaderboard read is never behind a verdict that is already stored. Judging
throughput is bounded by sandbox start-up — tens of milliseconds at best — so a row lock per
verdict is not the constraint that matters. If it ever became one, the counter would move to a
per-contest sequence or be derived from `max(problem_scores.updated_at)`, at the cost of a
weaker freshness signal.
