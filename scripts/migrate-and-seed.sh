#!/bin/sh
# Runs once per `docker compose up`, before the API is allowed to start.
set -eu

echo "[migrate] alembic upgrade head"
cd /srv/api
alembic upgrade head

echo "[migrate] seeding deterministic bundle"
cd /srv
python seed/seed.py

echo "[migrate] done"
