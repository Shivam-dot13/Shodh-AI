# Isolation probe: submitted code must never reach the Docker control socket.
import os
import sys

if os.path.exists("/var/run/docker.sock"):
    print("DOCKER_SOCKET_PRESENT")
    raise SystemExit(0)
print("DOCKER_SOCKET_ABSENT", file=sys.stderr)
raise SystemExit(5)
