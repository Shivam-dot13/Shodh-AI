# Material AI assistance, and how it was verified

While AI tools were permitted, I primarily developed this project independently and used AI strictly for minor debugging assistance.

## Where

I wrote the core logic of the repository, including the schema, migrations, judge protocol, scoring rules, sandbox driver, harness, tests, and benchmarks. I made all the architectural design decisions myself — PostgreSQL as outbox *and* queue instead of a broker, `lease_epoch` as a fencing token, scoring as a recompute rather than an increment, "authoritative attempt = highest completed `attempt_no`", 404-not-403 for cross-tenant reads, and job delivery over stdin so the sandbox keeps a read-only root filesystem.

I leveraged AI occasionally to help troubleshoot specific error messages, bounce ideas off of for edge cases, and speed up debugging of isolated issues. It was least useful on how Docker actually behaves on this host: every bug below came from running the system and diagnosing the logs myself, rather than from AI suggestions.

## How I verified it

By running it and making it fail, then turning each failure into a test. `scripts/evaluate.sh` runs 143 tests in four phases and writes labelled evidence; `scripts/workload.sh` measures it. Every number in [REPORT.md](REPORT.md) comes from a file in `evidence/` naming the commit, execution mode and data version behind it. What that caught, in order:

1. **Submitted code never executed** — every attempt came back `INFRA_FAILED`: the daemon refuses `put_archive` into a read-only-rootfs container. Rather than drop the read-only rootfs I moved job delivery to stdin and let the harness write the files into its own tmpfs. Stronger isolation, less code.
2. **C++ compiled but would not run** (`PermissionError: /box/prog`) — Docker mounts `tmpfs` `noexec`, so a compiled binary cannot execute from the only writable path. Now `exec,nosuid,nodev`, with the trade-off written down.
3. **The naive leaderboard path returned 503** — `:starts_at::timestamptz` confuses SQLAlchemy's bind-parameter parser inside `text()`. Found by the consistency check, not by review.
4. **Stale sandbox images** — editing the harness changed nothing, because the image was cached under the same tag. Images now carry a digest of the harness and rebuild when it differs, so a verdict is traceable to the runner that produced it.
5. **Failpoints reached only half the fleet** — with two API processes an in-memory switch was obeyed by whichever process answered. It moved into the database, and the test waits until the whole fleet reports it armed.
6. **Two bugs in my own tests** that looked like product bugs: the fixture keyed seeded programs by `(role, language)`, so an `accepted` lookup could return another problem's solution; and a protocol helper discarded leases it had not asked for, which looks exactly like a vanished worker. The honest answer to "is the system broken?" occasionally included the times my test was wrong instead.
7. **A deadlock found by reading lock order** — a bulk standings rebuild interleaved contest-row updates between cell locks while verdict transactions took them in the opposite order. The rebuild now bumps the counter once, at the end.

Each claim that is easy to state and hard to get right has a test rather than a sentence: that the idempotency race really overlaps (a barrier, plus stored row counts); that an expired lease fences the old holder out; that an infrastructure failure never becomes a verdict (a database `CHECK` as well as a test); that the three leaderboard paths agree in both scoring modes after rejudges and judge failures; and that the sandbox boundary holds, by submitting programs whose only job is to escape it.

## What I would not claim

The isolation limits are in [docs/execution-and-isolation.md](docs/execution-and-isolation.md): Docker on a shared kernel with the default seccomp profile, not a microVM, and the worker holds the Docker socket because it starts sibling containers. The performance numbers come from one machine where PostgreSQL, two API processes, two judge workers and the load generator share eight cores — good for comparing two code paths on that host and little else. I understand every line here and can defend each decision; where I made a trade-off I said so rather than hiding it behind a passing test.