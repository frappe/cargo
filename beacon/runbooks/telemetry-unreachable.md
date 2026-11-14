# Telemetry endpoint unreachable

Beacon cannot read `https://telemetry-svc.<wildcard domain>/health` of this region from outside for 3 minutes. No machine of the region can send telemetry, so the silent alerts follow. The alert detail shows the error or the status.

1. Open the URL in a browser. If DNS fails, select **Update DNS Records** on the Datum Server. If TLS fails, follow the certificate runbook.
2. If the request times out or returns 502, sign in to the Datum host and run `systemctl status caddy datum`. Start a unit that is down.
3. If Datum returns 503, it cannot reach ClickHouse. Follow the Datum ingest stopped runbook.
4. Data sent while the endpoint is down is lost: no sender retries.
