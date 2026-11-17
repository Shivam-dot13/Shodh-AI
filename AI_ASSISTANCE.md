# Material AI assistance, and how it was verified

AI tools were permitted, and I used them heavily. This is what they did and what I did.

## Where AI was used

I used Claude (via Claude Code) as a drafting and review tool across most of the repository:
the schema and migration, the judge protocol, the scoring rules, the sandbox driver and
harness, the test suites, the benchmark harnesses, and this documentation. I made the design
decisions — PostgreSQL as both outbox and queue instead of a broker, `lease_epoch` as a fencing
token, scoring as a recompute rather than an increment, "authoritative attempt = highest
completed attempt_no", 404-not-403 for cross-tenant reads, job delivery over stdin so the
sandbox can keep a read-only root filesystem — and then used AI to write them out, argue
against them, and find the holes.

Where it was most useful: writing a lot of correct boilerplate quickly (Pydantic contracts,
Alembic migration, route wiring), and producing a first draft of the SQL for the three
leaderboard paths. Where it was least useful: anything about how Docker actually behaves on
this host. Every one of the bugs below came from running the thing, not from reading it.

## How I verified it

Not by reading the diff. By running the system and making it fail, then making the failure a
test. The whole system runs under `docker compose up --build`; `scripts/evaluate.sh` runs 140
tests in four phases and writes labelled evidence; `scripts/workload.sh` measures it. Every
number in [REPORT.md](REPORT.md) comes from a file in `evidence/` that names the commit, the
execution mode and the data version it was produced under.

Concretely, verification caught these, in this order:

1. **Submitted code never executed at all.** Every attempt came back `INFRA_FAILED`:
   `put_archive` is refused by the daemon when the target container has `read_only=True`, which
   no amount of reading would have told me. Rather than give up the read-only root filesystem, I
   moved job delivery to the container's **stdin**, with the harness writing the files into its
   tmpfs itself. Stronger isolation *and* less code.
2. **C++ compiled but could not run** — `PermissionError: /box/prog`. Docker mounts `tmpfs`
   `noexec` by default, so a compiled binary cannot be executed from the only writable path. Now
   mounted `exec,nosuid,nodev`, and that trade-off is written down in
   [docs/execution-and-isolation.md](docs/execution-and-isolation.md).
3. **The naive leaderboard path 500'd.** `:starts_at::timestamptz` inside a SQLAlchemy `text()`
   confuses the bind-parameter parser; `CAST(:starts_at AS timestamptz)` is correct. Found by the
   consistency check, not by review.
4. **Stale sandbox images.** Editing the harness changed nothing, because the runner image was
   cached under the same tag. Images are now stamped with a digest of the harness and rebuilt
   when it does not match, so a verdict can always be traced to the runner that produced it.
5. **Failpoints only reached half the fleet.** The deployment runs two API processes, so an
   in-memory failpoint was obeyed by whichever process happened to answer. The switch moved into
   the database, and the test now waits until the whole fleet reports it armed. A fault only half
   the fleet obeys proves nothing.
6. **A test bug that looked like a product bug.** Correct programs were being judged
   `WRONG_ANSWER`. The fixture indexed seeded programs by `(role, language)`, and three problems
   all have an `accepted` program, so the lookup returned a solution to a different problem. The
   index is keyed by problem too now. Worth saying because the honest answer to "did the AI write
   working code?" includes the cases where the test was wrong rather than the code.
7. **An unreliable probe.** The C++ runtime-failure program divided by a `volatile` zero, which
   `-O2` can elide; it was passing tests instead of crashing. Replaced with a bounds-checked
   `std::vector::at()`, which throws and terminates deterministically.
8. **A dropped lease in a test helper.** The protocol helper claimed work it was not asking for
   and threw it away, which looks exactly like a vanished worker and broke the next test. It now
   parks extra leases and hands them out.

Beyond that, I checked the things that are easy to state and hard to get right, each with a
test rather than an assertion in prose: that the idempotency race really overlaps (a barrier,
and the stored row counts, not just the HTTP statuses); that an expired lease really fences the
old holder out (the zombie worker's result is refused with `409`); that an infrastructure
failure never becomes a verdict (a database `CHECK`, plus the test); that the three leaderboard
paths agree in both scoring modes after rejudges and judge failures; and that the sandbox
boundary holds by submitting programs whose only job is to try to escape it.

## What I would not claim

The isolation limits are stated plainly in
[docs/execution-and-isolation.md](docs/execution-and-isolation.md): this is Docker on a shared
kernel with the default seccomp profile, not a microVM, and the worker holds the Docker socket
because it starts sibling containers. The performance numbers are from one developer machine
under Docker Desktop where PostgreSQL, two API processes, two judge workers and the load
generator share the same CPUs; they are useful for comparing two code paths on that host and
not much else. I understand every line in this repository and can defend each of the decisions
above; where I made a trade-off I have said so rather than hiding it behind a passing test.
