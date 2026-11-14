# Object storage unreachable

Beacon cannot read `https://s3-admin-svc.<wildcard domain>/health` of this region from outside for 3 minutes. Customers cannot upload or download files. The alert detail shows the error or the status.

1. Open the URL in a browser. If DNS fails, select **Update DNS Records** on the Object Storage Cluster. If TLS fails, follow the certificate runbook.
2. If the request times out or returns 502, sign in to the gateway and run `systemctl status caddy garage`. Start a unit that is down.
3. If Garage returns 503, it has no quorum. Follow the object storage unavailable runbook.
4. Post an update on the incident, because customers see failed uploads.
