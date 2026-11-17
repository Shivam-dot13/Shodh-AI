# Runtime failure: raises before printing anything.
import sys

data = sys.stdin.read().split()
raise ValueError("deliberate runtime failure for the assessment seed")
