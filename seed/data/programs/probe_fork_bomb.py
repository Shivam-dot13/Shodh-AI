# Isolation probe: the process count is capped, so this cannot take the host down.
import os
import sys

spawned = 0
try:
    while spawned < 10_000:
        pid = os.fork()
        if pid == 0:
            while True:
                pass
        spawned += 1
except Exception as exc:
    print(f"FORK_CAPPED after {spawned}: {type(exc).__name__}", file=sys.stderr)
raise SystemExit(7)
