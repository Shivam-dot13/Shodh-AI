# Exceeds the time limit: busy loop, no output.
import time

start = time.monotonic()
total = 0
while True:
    total += 1
    if total % 5_000_000 == 0 and time.monotonic() - start > 600:
        break
