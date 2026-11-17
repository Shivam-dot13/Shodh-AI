#!/usr/bin/env bash
# The documented concurrent workload command.
#
#   bash scripts/workload.sh                      # defaults below
#   DURATION=120 CONCURRENCY=64 bash scripts/workload.sh
#
# Runs the same workload twice on purpose, so API overhead and program execution can be read
# apart rather than blended into one number:
#
#   1. api-only    judge workers stopped. Submissions are accepted and queued but nothing is
#                  executed, so the latencies are the API and the database alone.
#   2. end-to-end  judge workers running. The same request mix, now competing with real
#                  container start-ups on the same machine, plus queue delay and program time
#                  taken from the stored records.
#
# Then it measures the leaderboard bottleneck before and after on one larger dataset and
# captures a query plan for each path.
#
# Writes evidence/workload-api-only.json, evidence/workload-end-to-end.json and
# evidence/bottleneck-leaderboard.json.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="${COMPOSE:-docker compose}"
export COMMIT_SHA="${COMMIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"

DURATION="${DURATION:-60}"
CONCURRENCY="${CONCURRENCY:-32}"
PARTICIPANTS="${PARTICIPANTS:-120}"
PROBLEMS="${PROBLEMS:-5}"
PREFILL="${PREFILL:-3}"
SUBMIT_SHARE="${SUBMIT_SHARE:-0.3}"
BOTTLENECK_PARTICIPANTS="${BOTTLENECK_PARTICIPANTS:-400}"
BOTTLENECK_PROBLEMS="${BOTTLENECK_PROBLEMS:-6}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "building and starting (commit ${COMMIT_SHA})"
$COMPOSE --profile tools build
$COMPOSE up -d
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 120

say "run 1/3  api-only: judge workers stopped, nothing is executed"
$COMPOSE stop worker
$COMPOSE run --rm bench \
  --label api-only \
  --duration "$DURATION" --concurrency "$CONCURRENCY" \
  --participants "$PARTICIPANTS" --problems "$PROBLEMS" --prefill "$PREFILL" \
  --submit-share "$SUBMIT_SHARE"

say "run 2/3  end-to-end: judge workers running, real containers"
$COMPOSE start worker
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 60 >/dev/null
$COMPOSE run --rm bench \
  --label end-to-end \
  --duration "$DURATION" --concurrency "$CONCURRENCY" \
  --participants "$PARTICIPANTS" --problems "$PROBLEMS" --prefill "$PREFILL" \
  --submit-share "$SUBMIT_SHARE" --drain 180

say "run 3/3  leaderboard bottleneck, before and after, with query plans"
$COMPOSE run --rm --entrypoint python bench -m bench.bottleneck \
  --participants "$BOTTLENECK_PARTICIPANTS" --problems "$BOTTLENECK_PROBLEMS" --prefill "$PREFILL"

say "done"
echo "results are in ./evidence"
