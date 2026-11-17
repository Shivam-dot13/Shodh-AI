# Isolation probe: the root filesystem is read-only, so this must fail.
import sys

try:
    with open("/shodh_escape_marker", "w", encoding="utf-8") as handle:
        handle.write("escaped")
    print("ROOTFS_WRITABLE")
except Exception as exc:
    print("ROOTFS_READ_ONLY", type(exc).__name__, file=sys.stderr)
    raise SystemExit(4)
