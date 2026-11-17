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
BOTTLENECK_PREFILL="${BOTTLENECK_PREFILL:-1}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "building and starting (commit ${COMMIT_SHA})"
$COMPOSE --profile tools build
$COMPOSE up -d
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 120

# The datasets are built with the judge stopped and then reused, for two reasons: the history
# is written by one synthetic judge instead of racing the real one, and both measured runs then
# execute against byte-identical data.
say "preparing datasets (judge stopped so the history is written deterministically)"
$COMPOSE stop worker
rm -f evidence/.dataset-end-to-end.json evidence/.dataset-api-only.json
for LABEL in end-to-end api-only; do
  $COMPOSE run --rm bench \
    --label "$LABEL" --build-only --dataset-file "/srv/evidence/.dataset-$LABEL.json" \
    --participants "$PARTICIPANTS" --problems "$PROBLEMS" --prefill "$PREFILL"
done

say "run 1/3  end-to-end: judge workers running, real containers, empty queue"
$COMPOSE start worker
bash scripts/wait-for-api.sh "http://localhost:${API_PORT:-8000}" 60 >/dev/null
$COMPOSE run --rm bench \
  --label end-to-end --dataset-file /srv/evidence/.dataset-end-to-end.json \
  --duration "$DURATION" --concurrency "$CONCURRENCY" \
  --submit-share "$SUBMIT_SHARE" --drain 240

say "run 2/3  api-only: judge workers stopped, nothing is executed"
$COMPOSE stop worker
$COMPOSE run --rm bench \
  --label api-only --dataset-file /srv/evidence/.dataset-api-only.json \
  --duration "$DURATION" --concurrency "$CONCURRENCY" \
  --submit-share "$SUBMIT_SHARE"

# Still with the judge stopped: this run measures two read paths, so sandbox start-ups on the
# same host would only add noise to the comparison.
say "run 3/3  leaderboard bottleneck, before and after, with query plans"
$COMPOSE run --rm --entrypoint python bench -m bench.bottleneck \
  --participants "$BOTTLENECK_PARTICIPANTS" --problems "$BOTTLENECK_PROBLEMS" \
  --prefill "$BOTTLENECK_PREFILL"
$COMPOSE start worker

say "done"
echo "results are in ./evidence"
