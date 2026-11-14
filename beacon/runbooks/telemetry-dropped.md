# Telemetry dropped on machine

The Datum agent on the machine dropped 100 or more events in 1 hour. The alert key is the reason. The data of the machine has a gap, so a rule that reads it can miss a problem.

1. Read the drops: `SELECT labels, max(value) FROM datum.samples_recent WHERE metric = 'datum_agent_dropped_events_total' AND resource_id = '<machine>' GROUP BY labels`.
2. Reason `cap`: one service writes more than 1,000 log lines a minute. Find it: `SELECT service, count() FROM datum.logs WHERE resource_id = '<machine>' AND ts > now() - INTERVAL 1 HOUR GROUP BY service ORDER BY 2 DESC`. Fix the service, or drop its noisy lines in the role file. The lines stay in the local journal.
3. Reason `buffer_full`: the agent cannot send to Datum, and its disk buffer is full. Check `journalctl -u vector` on the machine, and the [Datum refuses writes](datum-writes-refused.md) and [Datum stores no new samples](datum-ingest-stopped.md) runbooks.
4. Reason `error`: a transform or a sink of the component fails. Read `journalctl -u vector` on the machine for the error.
