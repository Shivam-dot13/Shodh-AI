# Submission and attempt state model

Two objects have state, and keeping them apart is what makes the rest of the system simple.

* A **submission** is what a participant sent: contest, problem, language, source. It is
  immutable. Its `status` is derived, never set by a client.
* A **judging attempt** is one run of that submission against one pinned
  (`problem_version`, `test_suite`, `runtime_config`) triple. Attempts are append-only. A
  rejudge adds an attempt; it never edits one.

## Attempt states

```
                   lease                 heartbeat
   PENDING ───────────────► DISPATCHED ──────────────► RUNNING
      ▲                        │  │                     │  │
      │  lease expired,        │  │ result              │  │ result
      │  budget remaining      │  ▼                     │  ▼
      └────────────────────────┤ COMPLETED ◄────────────┘  │
                               │  (carries a verdict)      │
                               │                            │
                               ├──► INFRA_FAILED ◄──────────┘
                               │    (no verdict, ever)
                               └──► CANCELLED
```

| State          | Meaning                                                            | Terminal |
| -------------- | ------------------------------------------------------------------ | -------- |
| `PENDING`      | queued, waiting for a judge to claim it                            | no       |
| `DISPATCHED`   | leased by a worker, not yet reported as started                    | no       |
| `RUNNING`      | the worker has heartbeated; the program is being executed          | no       |
| `COMPLETED`    | a verdict was stored                                               | yes      |
| `INFRA_FAILED` | the judge could not judge; **no verdict, no score change**         | yes      |
| `CANCELLED`    | administratively stopped before judging                            | yes      |

Transitions are declared in `api/app/domain/enums.py` and asserted in
`tests/unit/test_state_model.py`. Anything not declared is refused by the judge plane with
`409 illegal_state_transition`.

Two transitions are worth pointing at:

* `DISPATCHED/RUNNING → PENDING` is lease expiry. The work goes back on the queue and the
  attempt's `lease_epoch` is incremented, which fences the previous holder out.
* `PENDING → COMPLETED` does not exist. A verdict requires a lease, which is what makes the
  fencing token meaningful.

## Verdicts

A verdict exists only on a `COMPLETED` attempt:

`ACCEPTED`, `WRONG_ANSWER`, `COMPILE_ERROR`, `RUNTIME_ERROR`, `TIME_LIMIT_EXCEEDED`,
`MEMORY_LIMIT_EXCEEDED`, `OUTPUT_LIMIT_EXCEEDED`.

A judge infrastructure failure is deliberately **not** in that list. It is a different state,
it carries `verdict = NULL`, and the database enforces both directions:

```sql
CHECK ((state <> 'COMPLETED') OR (verdict IS NOT NULL))
CHECK ((state <> 'INFRA_FAILED') OR (verdict IS NULL))
```

The verdict is derived by the API from the per-test outcomes the worker reports, not chosen by
the worker (`api/app/services/verdicts.py`). The first failing test *by ordinal* names the
verdict, so the answer does not depend on the order results arrive in. Partial credit is
weighted and is capped below `max_points`, so a non-accepted attempt can never score full
marks.

## Submission status

`status` is derived from the attempts on every write:

| Status         | When                                                                     |
| -------------- | ------------------------------------------------------------------------ |
| `QUEUED`       | the newest attempt is `PENDING` or `DISPATCHED`                          |
| `RUNNING`      | the newest attempt is `RUNNING`                                          |
| `JUDGED`       | at least one attempt is `COMPLETED`                                      |
| `JUDGE_FAILED` | the newest attempt is `INFRA_FAILED`/`CANCELLED` **and none ever completed** |

So a submission that was judged and is now being rejudged reads `QUEUED` with
`authoritative_attempt` still pointing at the verdict that currently counts; and a submission
whose rejudge hit a runner failure stays `JUDGED` on its earlier verdict. A runner crash never
costs a participant a score they already earned.

## The authoritative attempt

> The authoritative attempt of a submission is the `COMPLETED` attempt with the highest
> `attempt_no`.

Everything about ordering follows from that one rule:

* An `INFRA_FAILED` attempt is never authoritative, because it never completes.
* A result that arrives late from an attempt a rejudge has already replaced does get stored,
  history is preserved, but a higher `attempt_no` already completed, so the standings do not
  move. (`tests/integration/test_judge_protocol.py::test_a_late_result_from_a_replaced_attempt_does_not_move_the_standings`)
* Results completing out of order land in the same final state, because the standings cell is
  recomputed from the stored set of attempts rather than adjusted by arrival.

## Queue states

The queue is a table, not a broker. One row per attempt (`unique(attempt_id)`).

| State    | Meaning                                                                  |
| -------- | ------------------------------------------------------------------------ |
| `READY`  | claimable once `available_at` has passed                                 |
| `LEASED` | held by a worker until `lease_expires_at`                                |
| `DONE`   | the attempt finished, or the job was superseded by a rejudge             |
| `DEAD`   | the delivery budget (`max_deliveries`, default 3) was spent              |

Claiming is `SELECT ... FOR UPDATE SKIP LOCKED`; retries back off exponentially
(`min(30s, 2^delivery_count)`); expired leases are reclaimed by a reaper that runs in every API
process every two seconds, and by `POST /internal/judge/reap` for tests that want it to happen
now.
