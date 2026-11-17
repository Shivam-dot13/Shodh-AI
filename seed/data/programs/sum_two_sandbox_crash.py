# SHODH_FAILPOINT:runner_crash_in_container
# Correct code. The marker makes the in-container harness abort, which the worker must report
# as an infrastructure failure and never as a student verdict.
import sys

a, b = (int(x) for x in sys.stdin.read().split()[:2])
print(a + b)
