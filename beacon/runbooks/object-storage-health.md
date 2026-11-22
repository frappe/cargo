# Object storage cluster is unhealthy

Cargo reads the health of each object storage cluster from its gateway every minute. The cluster is Critical or Degraded (High), Unknown (Medium), or Cargo sent no verdict for 10 minutes (Medium).

1. In Cargo Desk, open the Object Storage Cluster. **Health Reason** names the machine and the problem.
2. For a machine that is down or full, follow the object storage unavailable or disk full runbooks.
3. For a gateway that does not answer on its public address, its nginx, certificate or public IPv4 is broken while Garage still runs. SSH to it and run `systemctl status nginx` and `nginx -t`, or run **Setup** again to reinstall nginx.
4. For Unknown, or no verdict, open the Error Log list and search for `Could not read`. The gateway may not answer, or the Cargo scheduler stopped.
5. Post an update on the incident when uploads or downloads fail for customers.
