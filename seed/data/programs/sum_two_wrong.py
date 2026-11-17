# Wrong output: off by one, so every test mismatches.
import sys

a, b = (int(x) for x in sys.stdin.read().split()[:2])
print(a + b + 1)
