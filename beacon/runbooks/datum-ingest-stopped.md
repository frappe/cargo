# Datum stores no new samples

The Datum of the region stored no new sample for 3 minutes. Every machine of the region sends each minute, so the region has no telemetry now. Beacon can still read the old data, so this rule runs.

1. On the Datum host, run `systemctl status datum caddy clickhouse-server`. Start a unit that is down.
2. Run `curl -s localhost:8000/health`. A 503 means that Datum cannot reach ClickHouse. Check `journalctl -u clickhouse-server -n 200` and the free disk space.
3. Read `journalctl -u datum -n 200` for refused requests. Many 401 answers mean that the tokens or the keys of Central changed.
4. When Datum works again, the machine-silent alerts resolve. Data for the gap is lost: no sender retries.
