#!/usr/bin/env bash
# One command for functional evaluation.
#
#   bash scripts/evaluate.sh
#
# Brings the whole system up, runs every suite in the right conditions, runs the end-to-end
# walkthrough, and writes labelled results under evidence/:
#
#   evidence/test-report-unit.json         rules in isolation, no database, no execution
#   evidence/test-report-functional.json   the running system, real workers, configured mode
#   evidence/test-report-realrunner.json   container isolation and resource limits
#   evidence/test-report-concurrency.json  the judge protocol, with the tests as the only judge
#   evidence/demo-<timestamp>.log          the walkthrough transcript
#   evidence/evaluation-summary.json       one file naming the commit, config and data version
#
# The concurrency phase stops the judge workers on purpose: those tests hold leases themselves
# and have to be the only judge on the queue. They are restarted afterwards.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="${COMPOSE:-docker compose}"
export COMMIT_SHA="${COMMIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
export EXEC_MODE="${EXEC_MODE:-docker}"
mkdir -p evidence

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "building images (commit ${COMMIT_SHA}, execution mode ${EXEC_MODE})"
$COMPOSE --profile tools build

say "starting postgres, migrations, seed, api, judge workers"
$COMPOSE up -d
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 120

say "phase 1/5  rules in isolation (unit)"
$COMPOSE run --rm -e RUN_LABEL=unit tests -m unit

say "phase 2/5  the running system (integration)"
$COMPOSE run --rm -e RUN_LABEL=functional tests -m "integration and not concurrency"

say "phase 3/5  container isolation and resource limits (realrunner)"
if [ "${EXEC_MODE}" = "docker" ]; then
  $COMPOSE run --rm -e RUN_LABEL=realrunner tests -m realrunner
else
  echo "skipped: EXEC_MODE=${EXEC_MODE} cannot demonstrate container isolation"
fi

say "phase 4/5  judge protocol under competing clients (concurrency, workers stopped)"
$COMPOSE stop worker
set +e
$COMPOSE run --rm -e RUN_LABEL=concurrency -e WORKERS_RUNNING=false tests -m concurrency
CONCURRENCY_STATUS=$?
set -e
$COMPOSE start worker
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 60 >/dev/null
if [ $CONCURRENCY_STATUS -ne 0 ]; then
  echo "concurrency phase failed with status $CONCURRENCY_STATUS" >&2
  exit $CONCURRENCY_STATUS
fi

say "phase 5/5  end-to-end walkthrough"
$COMPOSE run --rm --entrypoint python tests scripts/demo.py

say "writing evidence/evaluation-summary.json"
$COMPOSE run --rm --entrypoint python tests scripts/summarize_evidence.py

say "done"
echo "results are in ./evidence"
