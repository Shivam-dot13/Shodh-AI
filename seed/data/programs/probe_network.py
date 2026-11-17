# Isolation probe: the sandbox has no network, so this must fail.
import socket
import sys

socket.setdefaulttimeout(3)
try:
    sock = socket.create_connection(("1.1.1.1", 53), timeout=3)
    sock.close()
    print("NETWORK_REACHABLE")
except Exception as exc:
    print("NETWORK_BLOCKED", type(exc).__name__, file=sys.stderr)
    raise SystemExit(3)
