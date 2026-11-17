# Exceeds the output limit: prints far more than the problem allows.
import sys

line = "x" * 1024
for _ in range(4096):
    sys.stdout.write(line)
    sys.stdout.write("\n")
