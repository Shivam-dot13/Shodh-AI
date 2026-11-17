# Isolation probe: the sandbox runs as an unprivileged user, never root.
import os
import sys

print(f"uid={os.getuid()} gid={os.getgid()}", file=sys.stderr)
raise SystemExit(0 if os.getuid() != 0 else 6)
