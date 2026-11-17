#!/bin/sh
# Blocks until the API answers /readyz, then prints its version labels.
set -eu
BASE="${1:-http://localhost:8000}"
TRIES="${2:-60}"
i=0
while [ "$i" -lt "$TRIES" ]; do
  if curl -fsS "$BASE/readyz" >/dev/null 2>&1; then
    curl -fsS "$BASE/version"
    echo
    exit 0
  fi
  i=$((i+1))
  sleep 1
done
echo "API did not become ready at $BASE" >&2
exit 1
