# Telemetry volume grew

A sender sent more than 2 times its 7-day median of samples and log lines yesterday. The alert key is the resource ID of the sender. The data does not have a gap, but the storage cost grows.

1. Find the metrics that grew: `SELECT metric, count() FROM datum.samples_recent WHERE resource_id = '<sender>' AND ts > now() - INTERVAL 1 HOUR GROUP BY metric ORDER BY 2 DESC LIMIT 20`.
2. Find the logs that grew: `SELECT date, product, service, level, log_count FROM datum.daily_log_stats WHERE resource_id = '<sender>' AND date >= yesterday() - 7 ORDER BY date, log_count DESC`.
3. A new metric or label with many values: change the producer to send only what a rule, an SLI or a dashboard reads. See the telemetry contract in Beacon.
4. Many log lines from one service: fix the service, or send only its warning lines and higher.
5. A planned change, such as new machines: no action. The alert resolves 1 day after the volume is normal again.
