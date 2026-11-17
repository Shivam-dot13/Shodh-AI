# Exceeds the memory limit: allocates well past it.
blocks = []
while True:
    blocks.append(bytearray(8 * 1024 * 1024))
