# Verification: what is checked, how to run it, how to read the evidence

Every claim in this submission has a command behind it. Nothing in the test suite hardcodes a
verdict or a leaderboard: expected outcomes live in the test that asserts them, the seed data
only labels what each program is *for*, and the stored records are asserted alongside the HTTP
responses.

## One command

```bash
bash scripts/evaluate.sh
```

It builds, brings the system up, runs four phases under the conditions each one needs, runs the
end-to-end walkthrough, and writes labelled results into `evidence/`.

| Phase          | Marker        | Conditions                      | Count |
| -------------- | ------------- | ------------------------------- | ----- |
| rules          | `unit`        | no database, no API             | 47    |
| the system     | `integration` | real workers, configured mode   | 63    |
| the sandbox    | `realrunner`  | `EXEC_MODE=docker` only         | 19    |
| the protocol   | `concurrency` | **judge workers stopped**       | 11    |

The concurrency phase stops the workers on purpose: those tests hold leases and report results
themselves, so they have to be the only judge on the queue. They refuse to run otherwise
(`require_quiet_queue()`), rather than passing against a queue someone else is draining.
`evaluate.sh` restarts the workers afterwards.

Run a single phase by hand:

```bash
docker compose run --rm tests -m unit
docker compose run --rm tests -m "integration and not concurrency"
docker compose run --rm tests -m realrunner
docker compose stop worker
docker compose run --rm -e WORKERS_RUNNING=false tests -m concurrency
docker compose start worker
```

## The workload

```bash
bash scripts/workload.sh                              # defaults
DURATION=120 CONCURRENCY=64 bash scripts/workload.sh  # heavier
```

Three runs:

1. **api-only** — judge workers stopped. Submissions are accepted and queued but nothing is
   executed, so the latencies are the API and the database alone. This is how API overhead is
   measured separately from program execution.
2. **end-to-end** — the same request mix with the workers running, so the API competes with
   real container start-ups on the same host. Queue delay and program execution time are read
   from the stored records, not guessed from the client side.
3. **bottleneck** — one larger dataset, the leaderboard served two ways, latency percentiles
   and a query plan for each, and a correctness check afterwards.

## Scenario family coverage

| Family                      | Evidence                                                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------ |
| Normal flow                 | `test_normal_flow.py` — join, submit, judge, score, rank; identifier returned before judging; counters agree with the leaderboard; at least two judge processes took part |
| Retries and concurrency     | `test_idempotency.py` — eight overlapping requests on one key released by a barrier: exactly one creation, one attempt, one job; conflicting codes on one key; key scoped to actor and contest; replay survives contest close and still enforces auth |
| Retries and concurrency     | `test_judge_protocol.py` — two workers claiming the same job behind a barrier; duplicate acks; fencing after reclaim; out-of-order completion; late result from a replaced attempt; bounded deliveries; results outside the pinned suite refused |
| Recovery and rejudging      | `test_recovery.py` — a crash after commit still leaves a dispatchable job; a worker that dies before acknowledging yields exactly one verdict; a controllable runner failure is never a student verdict; nothing is left stuck |
| Recovery and rejudging      | `test_rejudge.py` — explicit version pair; earlier attempts preserved; publishing a version does not rewrite history; a harder suite can lower the verdict; rejudge is audited |
| Security and execution      | `test_authorization.py` — cross-organization denial (404 for every read and write), participant attempting thirteen organizer actions, another participant's work invisible, hidden data never served, credential planes separated |
| Security and execution      | `test_isolation.py` — no network, read-only rootfs, non-root, no Docker socket, pids cap, wall clock, memory, output limits, no leftover containers, runner image recorded |
| Performance and consistency | `bench/workload.py`, `bench/bottleneck.py`, `test_standings_consistency.py` — three independent leaderboard paths agree in both scoring modes, after rejudges and after judge failures; rebuilding a healthy contest changes nothing |

## Coordinating races without sleeping

* **Barriers.** Competing requests are released together with `threading.Barrier`
  (`test_simultaneous_use_of_one_key_creates_exactly_one_submission`,
  `test_two_competing_workers_never_get_the_same_job`), so they genuinely overlap.
* **Explicit lease expiry.** A dead worker is simulated by setting `lease_expires_at` into the
  past and calling `POST /internal/judge/reap`, which is deterministic. Waiting out a real
  30-second lease would turn a correctness test into a timing test.
* **Armed failpoints.** `POST /dev/failpoints` arms a named fault. The switch is stored in the
  database, so it applies to *every* API process — a fault only half the fleet obeys proves
  nothing. The test waits until the whole fleet reports it armed before relying on it.
* **Source markers.** A seeded program can carry `SHODH_FAILPOINT:runner_unavailable` or
  `SHODH_FAILPOINT:runner_crash_in_container`, which makes the judge fail on purpose. Only those
  two names are reachable that way and only while the dev fixture is enabled, so a participant
  cannot kill a worker or skip an acknowledgement.
* **Two real processes.** The default deployment is two API processes and two judge workers
  against one database, and the functional suite asserts that more than one judge actually took
  part.

## Reading `evidence/`

| File                              | What it holds                                                      |
| --------------------------------- | ------------------------------------------------------------------ |
| `evaluation-summary.json`         | start here: commit, execution mode, data version, every suite's totals and any failures, workload headlines |
| `seed-manifest.json`              | the data version, every identifier and fixture token, program roles  |
| `test-report-<phase>.json`        | per-test outcome, duration, markers, and the labels the phase ran under |
| `demo-<timestamp>.log`            | the end-to-end walkthrough transcript                               |
| `workload-api-only.json`          | API overhead with nothing executing                                 |
| `workload-end-to-end.json`        | the same mix with real judging, plus queue delay and program time    |
| `bottleneck-leaderboard.json`     | before/after latency, both query plans, and the correctness check    |

Every one of those files records the commit, the configured execution mode, the data version
and — for the test reports — whether the judge workers were running. No number in
[REPORT.md](../REPORT.md) is unattributable.

## Following one operation through the system

```bash
curl -sX POST $API/contests/$CONTEST/submissions -H "X-Request-Id: trace-me" ...
docker compose logs api worker | grep trace-me
```

The request id is echoed on the response, stored on the submission, handed to the worker with
the job and printed on every structured log line for the operation — through the API, the lease,
the sandbox and the verdict. Logs are JSON, and a fixed set of field names
(`authorization`, `token`, `password`, `source_code`, `expected_output`, `input_data`) is
dropped defensively, so an added log line cannot leak a credential or a hidden test by accident.
