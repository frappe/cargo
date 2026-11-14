# Datum refuses writes

Datum refused 10 or more requests of one sender in 15 minutes. No sender retries, so each refused request is a gap in its data. The alert key is the resource ID of the sender, or empty when the token was not valid.

1. Read the refused requests: `SELECT ts, attributes FROM datum.logs WHERE service = 'datum' AND message = 'request refused' ORDER BY ts DESC LIMIT 50`.
2. Status 401 or 403: the token of the sender expired, or its issuer has new keys. On a Cargo machine, run `systemctl start datum-refresh`. On an Atlas machine, install the Datum agent again from Atlas.
3. Status 400 or 422: the sender sends a value outside the contract, such as a bad level or metric name. Fix the sender. Datum refuses the whole batch for one bad line.
4. Status 429: the sender writes more than 12 times a minute on one route. Check that it has one agent only.
5. Status 5xx: Datum or ClickHouse fails. Follow the [Datum stores no new samples](datum-ingest-stopped.md) runbook.
