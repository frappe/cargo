# Object storage backup stale

The newest uploaded metadata backup of the object storage cluster is more than 1 day old. Garage takes a metadata snapshot every 6 hours, and Cargo uploads the newest one every hour. Without a recent backup, a lost metadata disk loses the index of every bucket.

1. In Cargo Desk, open the Error Log list and search for `could not upload metadata snapshot`. The title names the machine.
2. Open the Object Storage Cluster and check **Last Metadata Backup** and the health of each machine.
3. On the named machine, check that Garage writes snapshots to its metadata folder and that the disk has space.
4. Fix the cause, then wait for the next hourly upload, or run `cargo.object_storage.metadata_snapshot.upload_metadata_snapshots` from the console.
