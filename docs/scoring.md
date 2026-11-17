# Scoring, penalties and tie-breaking

The rules are pure functions of the authoritative judged attempts
(`api/app/services/scoring.py`), with no clock and no database access, so they are unit-tested
on their own (`tests/unit/test_scoring_rules.py`) and the same code backs both the live write
path and a full rebuild.

A contest picks one mode at creation time and keeps it.

## Definitions

* **Judged submission** — a submission whose authoritative attempt is `COMPLETED`. A submission
  still queued, or whose only attempt failed for judge-infrastructure reasons, does not take
  part in scoring at all.
* **Cell** — one `(contest, user, problem)` pair. Scoring is defined per cell, and the
  standings are the sum over a participant's cells.
* **Elapsed minutes** — `floor((submitted_at - contest.starts_at) / 60)`, never negative.
  The *submission* time is used, not the judging time, so a slow queue cannot cost a
  participant penalty minutes.

## Mode `ICPC` (default)

Per cell, taking the participant's submissions for that problem in `(submitted_at, id)` order:

```
first accepted submission?
  yes -> points   = max_points of the problem version that attempt was judged against
         solved   = true
         solved_at = that submission's submitted_at
         penalty  = elapsed_minutes(solved_at) + penalty_minutes * rejections_before_it
  no  -> points = 0, solved = false, penalty = 0
```

* `rejections_before_it` counts judged, non-accepted submissions *earlier in the order* than
  the accepted one. Submissions after the solve are ignored entirely.
* `penalty_minutes` is a per-contest setting (default 20).
* An unsolved problem contributes no penalty at all — penalty is only ever paid for a problem
  that was eventually solved.
* `COMPILE_ERROR` counts as a rejection, like any other non-accepted verdict. An
  `INFRA_FAILED` attempt does not, because it is not a verdict.

Totals: `solved` = number of solved cells, `penalty` = sum of penalties of solved cells,
`points` = sum of cell points.

**Ranking:** `solved` desc, then `penalty` asc, then `last_solved_at` asc, then `user_id` asc.

## Mode `POINTS`

Per cell: the best score over the participant's judged submissions for that problem; among
equal scores, the earliest submission wins. Partial credit comes from the per-test weights:

```
score = floor(max_points * passed_weight / total_weight)      (capped below max_points)
```

An accepted attempt scores exactly `max_points`. `penalty` is unused and stored as 0.

**Ranking:** `points` desc, then `last_progress_at` asc, then `user_id` asc.

## Tie-breaking and stability

Both orderings end in `user_id`, so the order is **total**: there is exactly one correct
ordering of any set of participants. That is what makes paging stable under concurrent writes
and what lets three independent implementations be compared byte for byte.

Ranks are competition ranks: equal scores share a rank and the next rank skips
(`1, 1, 3`). Only the score-significant columns take part in deciding a tie — `(solved,
penalty)` for ICPC, `(points,)` for POINTS — so two participants separated only by
`last_solved_at` share a rank but still appear in a fixed order.

## How a score is written

`api/app/services/scores.py` recomputes one cell inside the transaction that stores a verdict:

1. lock the cell row (`SELECT ... FOR UPDATE`, inserting it first if it is new);
2. read that cell's judged submissions;
3. apply the pure rules above and store the result;
4. increment `contests.standings_version`.

It is a recompute, not an increment. Three properties fall out of that:

* **Replaying a result changes nothing** — the same inputs give the same cell.
* **Arrival order does not matter** — the cell is a function of the stored set.
* **A rejudge needs no special case** — the newest completed attempt is simply part of the set.

## Submission eligibility

A submission is accepted only when all of these hold; each has its own error code:

| Rule                                        | Failure                              |
| ------------------------------------------- | ------------------------------------ |
| caller is a participant of the caller's org | `403 forbidden` / `404 not_found`    |
| caller has joined the contest               | `409 not_a_participant`              |
| `starts_at <= now`                          | `409 contest_not_started`            |
| `now < ends_at`                             | `409 contest_closed`                 |
| problem belongs to the contest              | `404 not_found`                      |
| problem has a published version with a suite| `422 invalid_request`                |
| under `max_attempts_per_problem`, if set    | `409 attempt_limit_reached`          |
| idempotency key is present and well formed  | `422 invalid_request`                |
| key not reused with a different request     | `409 idempotency_key_conflict`       |

A **replay** of an accepted submission (same key, same fingerprint, same actor, same contest)
returns the original submission with `200` even after the contest has closed. Retrying is not a
new submission, and a client must not be punished for a network failure it did not cause.

## Contest window

`starts_at` and `ends_at` are explicit, stored with time zone, and compared against the
database clock. Before `starts_at` participants cannot list problems or submit; at `ends_at`
submission stops. Reading a leaderboard, a submission or an attempt is unaffected — a closed
contest stays readable.
