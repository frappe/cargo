# Object storage errors

More than 1% of S3 requests to Garage failed with a 5xx status in 10 minutes.

1. Check the Object storage unavailable and Object storage redundancy lost alerts. A down node causes most errors.
2. Read `journalctl -u garage -n 200` on the gateway nodes for the failing endpoint.
3. Check the Caddy gateway in front of Garage for upstream errors.
