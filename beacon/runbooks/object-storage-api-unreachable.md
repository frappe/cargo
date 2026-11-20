# Object storage API unreachable

Beacon sends an unsigned request to `https://s3-svc.<wildcard domain>/beacon-probe/` of this region each minute, and Garage does not answer with 403 for 3 minutes. Customers cannot upload or download files. The alert detail shows the error or the status.

1. Run `curl -i https://s3-svc.<wildcard domain>/beacon-probe/`. If DNS fails, select **Update DNS Records** on the Object Storage Cluster. If TLS fails, follow the certificate runbook.
2. If the request times out or returns 502, sign in to each gateway and run `systemctl status nginx garage`. Start a unit that is down.
3. If Garage returns 503, it has no quorum. Follow the object storage unavailable runbook.
4. Post an update on the incident, because customers see failed uploads.
