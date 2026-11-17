# Shodh a Code — contest platform backend

A coding contest backend with an asynchronous judge. Participants join contests and submit
programs; submissions are accepted and queued immediately, judged in locked-down containers,
and scored onto a leaderboard that stays consistent under retries, competing workers,
out-of-order results and rejudges.

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 + Alembic · Docker.

```bash
docker compose up --build
```

That migrates the schema, loads a deterministic seed bundle, and starts **two API processes**
and **two judge workers** against one database. Then:

```bash
bash scripts/evaluate.sh     # one command: every suite, the walkthrough, labelled evidence
bash scripts/workload.sh     # the concurrent workload, twice, plus the bottleneck study
```

* API docs: <http://localhost:8000/docs> · OpenAPI: <http://localhost:8000/openapi.json>
* Build and configuration labels: <http://localhost:8000/version>
* Fixture identities and tokens: `evidence/seed-manifest.json`

---

## How it fits together

```
     participants / organizers                            judge workers (x2)
                │                                                 │
          bearer token                                   judge service token
        scope: user                                      scope: judge
                │                                                 │
                ▼                                                 ▼
    ┌───────────────────────────┐                   ┌──────────────────────────────┐
    │  FastAPI  (x2 processes)  │                   │  /internal/judge/*           │
    │  contests, problems,      │◄──────────────────│  lease → heartbeat → result  │
    │  submissions, standings   │   HTTP, separate  │  lease_epoch fences stale    │
    └───────────┬───────────────┘   auth plane      │  holders out                 │
                │                                   └──────────────┬───────────────┘
                │ one transaction:                                 │ docker socket
                │ submission + attempt + queue row                  │ (worker only)
                ▼                                                  ▼
    ┌─────────────────────────────────────┐          ┌───────────────────────────────┐
    │ PostgreSQL                          │          │ sandbox container per attempt │
    │  judge_jobs   outbox + work queue   │          │  no network, non-root,        │
    │               FOR UPDATE SKIP LOCKED│          │  read-only rootfs, tmpfs /box │
    │  problem_scores  standings cells,   │          │  cpu/mem/pids/wall/output caps│
    │               written in the verdict│          │  no host mounts, no socket    │
    │               transaction           │          └───────────────────────────────┘
    └─────────────────────────────────────┘
```

Four design decisions carry most of the weight:

**The queue is a table, not a broker.** `judge_jobs` is a transactional outbox and the work
queue at once: the queue row is inserted in the same transaction as the submission and its
first attempt. The window between "submission committed" and "job dispatched" — the one Stage 3
asks about — does not exist, because they are the same commit. Claiming is
`SELECT ... FOR UPDATE SKIP LOCKED`, so correctness across workers is a database property, not
an application one. No Redis, no RabbitMQ: nothing was added that the problem did not need.

**`lease_epoch` is a fencing token.** Every lease and every reclaim increments it, and a result
is accepted only at the attempt's current epoch. A worker that was paused, partitioned or
declared dead cannot overwrite the delivery that replaced it. Delivery is at-least-once; the
*effect* is exactly-once.

**Scoring is a recompute, never an increment.** A verdict transaction locks one
`(contest, user, problem)` cell and recomputes it from that cell's authoritative attempts using
the same pure functions a full rebuild uses. Replays are free, arrival order is irrelevant, and
double-scoring is not expressible.

**The authoritative attempt of a submission is its highest-numbered `COMPLETED` attempt.** That
single rule gives rejudges, late results and judge failures their behaviour: an infrastructure
failure never becomes authoritative (so a runner crash cannot cost a participant a score), and
a late result from a replaced attempt is stored as history without moving the standings.

## Reading the code

| Path                             | What is in it                                                        |
| -------------------------------- | -------------------------------------------------------------------- |
| `api/app/models.py`              | the schema, with the invariants it enforces written down             |
| `api/app/services/judge_queue.py`| the judge protocol: lease, heartbeat, result, failure, reclaim       |
| `api/app/services/scoring.py`    | contest rules as pure functions (no clock, no database)              |
| `api/app/services/scores.py`     | applying a verdict to the standings                                  |
| `api/app/services/standings.py`  | the three leaderboard paths: serving, naive, rebuild                 |
| `api/app/services/access.py`     | tenant-scoped lookup; authorization that a route cannot forget       |
| `worker/runners.py`              | the sandbox, and what it does and does not guarantee                 |
| `runner-images/harness.py`       | the in-container harness; measures, never judges                     |
| `seed/seed.py`                   | the deterministic seed bundle                                        |
| `bench/`                         | the workload harness and the bottleneck study                        |

## Documentation

| Document                                                      | Covers                                            |
| ------------------------------------------------------------- | ------------------------------------------------- |
| [REPORT.md](REPORT.md)                                        | architecture, transactions, performance evidence, limitations |
| [docs/api.md](docs/api.md)                                    | every route, request examples, pagination, correlation |
| [docs/scoring.md](docs/scoring.md)                            | scoring, penalties, tie-breaking, eligibility     |
| [docs/authorization.md](docs/authorization.md)                | scopes, roles, tenancy, hidden data, error shape  |
| [docs/state-model.md](docs/state-model.md)                    | submission and attempt states, verdicts, queue states |
| [docs/transactions.md](docs/transactions.md)                  | every write path, its boundary and the constraint behind each invariant |
| [docs/execution-and-isolation.md](docs/execution-and-isolation.md) | the sandbox, and its limits stated plainly   |
| [docs/verification.md](docs/verification.md)                  | what is tested, how to run it, how to read the evidence |
| [AI_ASSISTANCE.md](AI_ASSISTANCE.md)                          | where AI was used and how each part was verified  |

## Try it by hand

```bash
API=http://localhost:8000
ROHAN=shodh_tok_acme_rohan          # participant, Acme University
ORGANIZER=shodh_tok_acme_organizer  # organizer, Acme University

CONTEST=$(curl -s -H "Authorization: Bearer $ROHAN" $API/contests \
          | python -c "import json,sys; print(json.load(sys.stdin)['items'][0]['id'])")
PROBLEM=$(curl -s -H "Authorization: Bearer $ROHAN" $API/contests/$CONTEST/problems \
          | python -c "import json,sys; print(json.load(sys.stdin)['items'][0]['id'])")

curl -sX POST -H "Authorization: Bearer $ROHAN" $API/contests/$CONTEST/participants/me

curl -sX POST $API/contests/$CONTEST/submissions \
  -H "Authorization: Bearer $ROHAN" -H "Content-Type: application/json" \
  -H "Idempotency-Key: readme-1" -H "X-Request-Id: readme-1" \
  -d '{"problem_id":"'$PROBLEM'","language":"python3",
       "source_code":"import sys\na,b=map(int,sys.stdin.read().split()[:2])\nprint(a+b)\n"}'

# poll until status is JUDGED, then look at the leaderboard
curl -s -H "Authorization: Bearer $ROHAN" $API/contests/$CONTEST/leaderboard
```

Or run the whole walkthrough — join, ten submissions covering every outcome, idempotency,
rejudge, consistency check, authorization denials:

```bash
docker compose run --rm --entrypoint python tests scripts/demo.py
```

Seeded identities (tokens are fixtures; they are stored hashed):

| Organization        | Identity                   | Role        | Token                           |
| ------------------- | -------------------------- | ----------- | ------------------------------- |
| acme-university     | `organizer@acme.test`      | organizer   | `shodh_tok_acme_organizer`      |
| acme-university     | `rohan@acme.test`          | participant | `shodh_tok_acme_rohan`          |
| acme-university     | `meera@acme.test`          | participant | `shodh_tok_acme_meera`          |
| acme-university     | `dev@acme.test`            | participant | `shodh_tok_acme_dev`            |
| borealis-institute  | `organizer@borealis.test`  | organizer   | `shodh_tok_borealis_organizer`  |
| borealis-institute  | `lin@borealis.test`        | participant | `shodh_tok_borealis_lin`        |
| borealis-institute  | `sara@borealis.test`       | participant | `shodh_tok_borealis_sara`       |
| —                   | judge worker plane         | service     | `shodh_judge_dev_token`         |
| —                   | platform bootstrap         | service     | `shodh_platform_admin_dev_token`|

Contests: `autumn-open` (Acme, ICPC, open), `winter-sprint` (Borealis, POINTS, open),
`spring-archive` (Acme, closed, two-attempt cap). Problems: `sum-two`, `max-in-list`,
`word-count`. The seed is deterministic — identifiers are `uuid5` of stable names, so a reseed
produces the same ids — and it writes `evidence/seed-manifest.json`, which the tests, the demo
and the benchmarks all read instead of hardcoding anything.

Nothing in the system is specific to those fixtures: a platform-admin credential can create an
organization, identities, contests and problems the code has never seen and run the whole flow
through the public contracts, which is what
`tests/integration/test_contract_rules.py::TestUnseenData` does on every run.

## Verifying it

```bash
bash scripts/evaluate.sh
```

| Phase            | Condition                                        | Asserts                                            |
| ---------------- | ------------------------------------------------ | -------------------------------------------------- |
| `unit` (47)      | no database, no API                              | scoring, verdict mapping, comparison, state model  |
| `functional` (70)| the running system, real workers                 | the normal flow, contracts, identities, authorization, idempotency, rejudge, standings consistency, recovery |
| `realrunner` (19)| `EXEC_MODE=docker` only                          | network, rootfs, uid, socket, pids, CPU, memory, output limits, cleanup, both languages |
| `concurrency` (12)| workers stopped; the tests are the only judge   | competing claims, fencing, duplicate acks, out-of-order completion, bounded retries, late replaced results, contest-scoped claims |

Results land in `evidence/` as JSON, each labelled with the commit, the configured execution
mode, the data version and whether the judge workers were running. Details and how to read them:
[docs/verification.md](docs/verification.md).

```bash
bash scripts/workload.sh
```

Runs the same mixed workload twice — once with the judge workers stopped, so the latencies are
API and database only; once with them running, so queue delay and program execution show up
separately — then measures the leaderboard bottleneck before and after with a query plan for
each path. Results: `evidence/workload-*.json`, `evidence/bottleneck-*.json`.

## Configuration

Defaults work; `.env.example` lists everything.

| Variable                                   | Default  | Meaning                                              |
| ------------------------------------------ | -------- | ---------------------------------------------------- |
| `EXEC_MODE`                                | `docker` | `docker` (real isolation) or `local` (no container)  |
| `API_WORKERS` / `WORKER_REPLICAS`          | `2` / `2`| API processes and judge workers                      |
| `JUDGE_CONCURRENCY`                        | `2`      | attempts judged at once per worker                   |
| `LEASE_SECONDS` / `MAX_DELIVERIES`         | `30` / `3`| lease length and delivery budget                    |
| `DEV_AUTH_ENABLED`                         | `true`   | the token fixture and the failpoint routes           |
| `WORKER_FAILPOINTS`                        | unset    | e.g. `crash_before_result_ack`, `runner_unavailable` |
| `POSTGRES_PORT` / `API_PORT`               | `55432` / `8000` | host ports                                   |

## Repository layout

```
api/            FastAPI application, schema, migrations, services
worker/         judge worker: lease loop, sandbox drivers, comparison
runner-images/  per-language sandbox images and the in-container harness
seed/           deterministic seed bundle (data + programs covering every outcome)
tests/          unit · integration · realrunner · concurrency
bench/          workload harness, dataset builder, bottleneck study
scripts/        evaluate.sh · workload.sh · demo.py · migrate-and-seed.sh
docs/           API, scoring, authorization, state model, isolation, verification
evidence/       labelled results, written by the scripts above
```

One container image serves three roles (api, worker, tests), so every process in a measurement
shares one dependency set and one commit label.

## Known limitations

Collected honestly at the end of [REPORT.md](REPORT.md#known-limitations); the short version:
Docker is the isolation boundary (not a VM), `standings_version` serialises verdict commits per
contest, the leaderboard is polled rather than pushed, the authentication fixture is a fixture,
and the numbers in the report were measured on one developer machine under Docker Desktop,
where the database, both API processes, both judge workers and the load generator share the
same CPUs.
