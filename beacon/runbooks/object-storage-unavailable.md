# Object storage unavailable

Garage reports that the object storage cluster is not available: some partitions have no write quorum. Backups, site files and Pilot images fail.

1. On a storage node, run `garage status` and `garage stats`. Find the nodes that are down.
2. Start each node that is down. Check `systemctl status garage` and `journalctl -u garage -n 200`.
3. If a node is lost, change the layout with `garage layout` so the other nodes take its partitions.
4. Post an update on the incident. Customers see failed uploads and downloads.
