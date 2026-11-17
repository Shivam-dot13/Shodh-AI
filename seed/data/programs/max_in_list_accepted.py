# Correct solution for max-in-list.
import sys

data = sys.stdin.read().split()
count = int(data[0])
values = [int(x) for x in data[1 : 1 + count]]
print(max(values))
