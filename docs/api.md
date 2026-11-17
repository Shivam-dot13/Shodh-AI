# API

Machine-readable contracts: `GET /openapi.json` on a running system, and a captured copy at
[openapi.json](openapi.json). Interactive docs at `/docs`.

Everything below is a real request against a freshly seeded system. Fixture tokens come from
`evidence/seed-manifest.json`; `scripts/demo.py` runs this whole sequence and saves the
transcript under `evidence/`.

```bash
API=http://localhost:8000
ORGANIZER=shodh_tok_acme_organizer
ROHAN=shodh_tok_acme_rohan
CONTEST=$(curl -s -H "Authorization: Bearer $ROHAN" "$API/contests" | python -c "import json,sys;print(json.load(sys.stdin)['items'][0]['id'])")
```

## Routes

| Method | Path                                          | Who              | Purpose                                     |
| ------ | --------------------------------------------- | ---------------- | ------------------------------------------- |
| GET    | `/healthz` `/readyz` `/version`               | anyone           | liveness, readiness, build and config labels |
| POST   | `/auth/dev/token`                             | anyone (dev)     | mint a token for a seeded identity           |
| GET    | `/me`                                         | any credential   | who this credential is                       |
| POST   | `/orgs`                                       | platform admin   | create an organization                       |
| POST   | `/orgs/{id}/users`                            | admin, organizer | create an identity (returns its token)       |
| GET    | `/contests`                                   | user             | list contests of the caller's organization   |
| POST   | `/contests`                                   | organizer        | create a contest                             |
| GET    | `/contests/{id}`                              | user             | read a contest                               |
| PATCH  | `/contests/{id}`                              | organizer        | amend title, window, penalty, attempt cap    |
| POST   | `/contests/{id}/participants/me`              | participant      | join (idempotent)                            |
| GET    | `/contests/{id}/participants`                 | organizer        | list participants                            |
| POST   | `/contests/{id}/problems`                     | organizer        | add a problem with version 1 and a suite     |
| GET    | `/contests/{id}/problems`                     | user             | list problems                                |
| GET    | `/problems/{id}`                              | user             | statement and sample tests                   |
| POST   | `/problems/{id}/versions`                     | organizer        | publish a new version                        |
| GET    | `/problems/{id}/versions`                     | organizer        | list versions                                |
| POST   | `/problems/{id}/test-suites`                  | organizer        | add a suite version                          |
| GET    | `/problems/{id}/test-suites`                  | organizer        | list suites (counts only)                    |
| POST   | `/contests/{id}/submissions`                  | participant      | submit (idempotent)                          |
| GET    | `/contests/{id}/submissions`                  | user             | list (own only, for a participant)           |
| GET    | `/submissions/{id}`                           | owner, organizer | status, attempts, per-test outcomes          |
| GET    | `/submissions/{id}/attempts`                  | organizer        | judge-level detail                           |
| POST   | `/submissions/{id}/rejudge`                   | organizer        | rejudge against a chosen version pair        |
| POST   | `/problems/{id}/rejudge`                      | organizer        | rejudge every submission of a problem        |
| GET    | `/contests/{id}/leaderboard`                  | user             | standings (`source=standings\|naive\|rebuild`) |
| GET    | `/contests/{id}/standings/verify`             | organizer        | assert the three paths agree                 |
| POST   | `/contests/{id}/standings/rebuild`            | organizer        | recompute every cell from records            |
| GET    | `/contests/{id}/standings/explain`            | organizer        | `EXPLAIN (ANALYZE, BUFFERS)` for a path      |
| GET    | `/contests/{id}/stats`                        | organizer        | counters to cross-check the leaderboard      |
| GET    | `/ops/queue`                                  | organizer        | queue depth by state                         |
| POST   | `/dev/failpoints`                             | organizer (dev)  | arm or disarm a failpoint                    |
| POST   | `/internal/judge/leases`                      | judge            | claim work                                   |
| GET    | `/internal/judge/attempts/{id}/payload`        | judge            | re-read a held job                           |
| POST   | `/internal/judge/attempts/{id}/heartbeat`      | judge            | extend a lease                               |
| POST   | `/internal/judge/attempts/{id}/result`         | judge            | report a result (idempotent at one epoch)    |
| POST   | `/internal/judge/attempts/{id}/infra-failure`   | judge            | report that judging was impossible           |
| POST   | `/internal/judge/reap`                        | judge            | reclaim expired leases now                   |

## Join and submit

```bash
curl -sX POST -H "Authorization: Bearer $ROHAN" "$API/contests/$CONTEST/participants/me"
```

```bash
curl -sX POST "$API/contests/$CONTEST/submissions" \
  -H "Authorization: Bearer $ROHAN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-first-submission" \
  -H "X-Request-Id: demo-001" \
  -d '{"problem_id": "'"$PROBLEM"'", "language": "python3",
       "source_code": "import sys\na,b=map(int,sys.stdin.read().split()[:2])\nprint(a+b)\n"}'
```

`201` with the identifier, before any judging has happened:

```json
{
  "id": "0c8f...", "status": "QUEUED", "replayed": false,
  "latest_attempt": {"attempt_no": 1, "state": "PENDING", "verdict": null, "exec_mode": null},
  "authoritative_attempt": null
}
```

Repeating the call with the same key returns `200` and `"replayed": true`. Repeating it with
different code returns `409 idempotency_key_conflict`.

## Watch it finish

```bash
curl -s -H "Authorization: Bearer $ROHAN" "$API/submissions/$ID"
```

```json
{
  "status": "JUDGED",
  "authoritative_attempt": {
    "attempt_no": 1, "state": "COMPLETED", "verdict": "ACCEPTED",
    "score": 100, "max_score": 100, "tests_passed": 3, "tests_total": 3,
    "time_ms": 34, "memory_kb": 9216, "exec_mode": "docker"
  },
  "test_results": [
    {"ordinal": 1, "is_sample": true,  "status": "PASSED", "time_ms": 12, "output_bytes": 2, "truncated": false},
    {"ordinal": 2, "is_sample": false, "status": "PASSED", "time_ms": 11, "output_bytes": 7, "truncated": false}
  ]
}
```

No hidden input, no expected output, no program output — status and resource usage only.

## Leaderboard

```bash
curl -si -H "Authorization: Bearer $ROHAN" "$API/contests/$CONTEST/leaderboard?limit=20"
```

```
ETag: W/"42"
X-Standings-Source: standings
X-Compute-Ms: 3.41
X-Execution-Mode: docker
```

```json
{
  "scoring_mode": "ICPC", "source": "standings", "standings_version": 42,
  "execution_mode": "docker",
  "items": [
    {"rank": 1, "display_name": "Rohan Kumar", "solved": 1, "points": 100, "penalty_minutes": 93,
     "cells": [{"problem_id": "...", "points": 100, "solved": true, "penalty_minutes": 93,
                "rejected_before_solve": 0}]}
  ],
  "page": {"limit": 20, "offset": 0, "total": 3, "returned": 3}
}
```

`standings_version` is incremented inside the transaction that stores a verdict, and the `ETag`
is that counter. Clients poll and send `If-None-Match`; an unchanged leaderboard answers `304`.
There is no asynchronous refresh anywhere in the write path, so a `200` is never behind a
verdict that is already stored.

## Rejudge against an explicit version pair

```bash
curl -sX POST "$API/submissions/$ID/rejudge" \
  -H "Authorization: Bearer $ORGANIZER" -H "Content-Type: application/json" \
  -d '{"problem_version_id": "...", "test_suite_id": "..."}'
```

`202` with the new attempt. Earlier attempts stay readable through
`GET /submissions/{id}/attempts`, each naming the exact version, suite and runtime config it
ran against.

## Pagination and ordering

Every list answers `{"items": [...], "page": {"limit", "offset", "total", "returned"}}` with
`limit` 1..200 (500 for the leaderboard) and `offset` validated; out-of-range values are `422`.
Orderings are total, so paging cannot skip or repeat a row under concurrent writes:

| List          | Order                                                             |
| ------------- | ----------------------------------------------------------------- |
| contests      | `starts_at` desc, `id`                                            |
| problems      | `ordinal`, `id`                                                   |
| submissions   | `submitted_at` desc, `id` desc                                    |
| participants  | `joined_at`, `id`                                                 |
| leaderboard   | mode-specific score columns, then the tie-breakers, then `user_id` |

## Correlation

Send `X-Request-Id` and it is echoed on the response, stored on the submission, handed to the
worker with the job, and printed in every structured log line for that operation:

```bash
docker compose logs api worker | grep demo-001
```

```json
{"ts": "...", "level": "INFO", "message": "submission accepted", "request_id": "demo-001",
 "submission_id": "0c8f...", "attempt_id": "7b21..."}
{"ts": "...", "level": "INFO", "message": "judging started", "request_id": "demo-001",
 "attempt_id": "7b21...", "worker_id": "worker-1", "lease_epoch": 1, "exec_mode": "docker"}
{"ts": "...", "level": "INFO", "message": "verdict stored", "request_id": "demo-001",
 "attempt_id": "7b21...", "verdict": "ACCEPTED", "score": 100}
```
