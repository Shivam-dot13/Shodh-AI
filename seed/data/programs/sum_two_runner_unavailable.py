# SHODH_FAILPOINT:runner_unavailable
# Correct code. The marker makes the judge report an infrastructure failure instead of a
# verdict, so recovery can be exercised without waiting for a real crash.
import sys

a, b = (int(x) for x in sys.stdin.read().split()[:2])
print(a + b)
