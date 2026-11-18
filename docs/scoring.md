# Scoring, penalties and tie-breaking

The rules live in `api/app/services/scoring.py` as pure functions. No clock, no database, so
they can be unit-tested on their own (`tests/unit/test_scoring_rules.py`) and the same code runs
in both the live write path and a full rebuild.

## Definitions

**Judged submission.** A submission whose authoritative attempt is `COMPLETED`. One still
queued, or whose only attempt died for judge-infrastructure reasons, does not count at all.

**Cell.** One `(contest, user, problem)` pair. Scoring is defined per cell; a participant's
standing is the sum over their cells.

**Elapsed minutes.** `floor((submitted_at - contest.starts_at) / 60)`, never negative. It uses
the submission time, not the judging time, so a slow queue cannot cost anyone penalty minutes.

## The rules

ICPC style. For one cell, taking that participant's submissions for the problem in
`(submitted_at, id)` order:

```
first accepted submission?
  yes -> points    = max_points of the problem version that attempt was judged against
         solved    = true
         solved_at = that submission's submitted_at
         penalty   = elapsed_minutes(solved_at) + penalty_minutes * rejections before it
  no  -> points = 0, solved = false, penalty = 0
```

`rejections before it` counts judged, non-accepted submissions earlier in that order.
Submissions after the solve are ignored. `penalty_minutes` is a per-contest setting, 20 by
default.

An unsolved problem adds no penalty. Penalty is only ever paid for a problem that was
eventually solved.

`COMPILE_ERROR` counts as a rejection, like any other non-accepted verdict. An `INFRA_FAILED`
attempt does not, because it is not a verdict.

Totals: `solved` is the number of solved cells, `penalty` the sum of penalties of solved cells,
`points` the sum of cell points.

**Ranking:** `solved` desc, `penalty` asc, `last_solved_at` asc, `user_id` asc.

## Partial credit

An attempt also stores a weighted partial score, floored, and capped below `max_points` so a
non-accepted attempt can never show full marks:

```
score = floor(max_points * passed_weight / total_weight)
```

That number appears on the attempt, and is useful when looking at why something failed. It does
not feed the standings: under ICPC rules a cell scores `max_points` or nothing.

## Ties

Both the ranking and the per-cell ordering end in an id, so the order is total: there is exactly
one correct ordering of any set of participants. That is what makes paging stable while verdicts
are landing, and what lets three independent implementations be compared byte for byte.

Ranks are competition ranks: equal scores share a rank and the next one skips, so `1, 1, 3`.
Only `(solved, penalty)` decides a tie. Two participants separated only by `last_solved_at`
share a rank but still come back in a fixed order.

## When a submission is accepted

All of these must hold, and each has its own error code:

| Rule | Failure |
| --- | --- |
| caller is a participant in their own organization | `403 forbidden` or `404 not_found` |
| caller has joined the contest | `409 not_a_participant` |
| `starts_at <= now` | `409 contest_not_started` |
| `now < ends_at` | `409 contest_closed` |
| problem belongs to the contest | `404 not_found` |
| problem has a published version with a suite | `422 invalid_request` |
| under `max_attempts_per_problem`, if set | `409 attempt_limit_reached` |
| idempotency key present and well formed | `422 invalid_request` |
| key not reused with a different request | `409 idempotency_key_conflict` |

A replay (same key, same fingerprint, same actor, same contest) returns the original submission
with `200`, even after the contest has closed. Retrying is not a new submission, and a client
should not be punished for a network failure it did not cause.

## The contest window

`starts_at` and `ends_at` are explicit, stored with time zone, compared against the database
clock. Before `starts_at` participants cannot list problems or submit. At `ends_at` submission
stops. Reading a leaderboard, a submission or an attempt still works: a closed contest stays
readable.

## How a score gets written

`api/app/services/scores.py` recomputes one cell inside the transaction that stores the verdict:

1. lock the cell row (`SELECT ... FOR UPDATE`, inserting it first if new);
2. read that cell's judged submissions;
3. apply the rules above and store the result;
4. bump `contests.standings_version`.

It recomputes, it never increments. Three things follow from that. Replaying a result changes
nothing, because the same inputs give the same cell. Arrival order does not matter, because the
cell is a function of the stored set. And a rejudge needs no special case, because the newest
completed attempt is simply part of that set.
