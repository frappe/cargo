# Object storage disk almost full

A data or metadata disk of a Garage node has less than 10% free space. Garage stops writes when a disk is full.

1. On the node, run `df -h` and `garage stats`.
2. If the metadata disk is full, compact or move the metadata database.
3. If the data disk is full, add capacity to the node or add a node and change the layout.
4. Look for buckets that grew fast, for example an unexpected backup loop.
