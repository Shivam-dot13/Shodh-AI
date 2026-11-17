# Correct unless the sum is negative, so some tests pass and some do not.
import sys

a, b = (int(x) for x in sys.stdin.read().split()[:2])
total = a + b
print(total if total >= 0 else 0)
