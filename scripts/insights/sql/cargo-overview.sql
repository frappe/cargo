WITH
latest AS (
  SELECT metric, labels['service'] AS service, argMax(value, ts) AS value, max(ts) AS at
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND ts > now() - INTERVAL 5 MINUTE
    AND metric IN ('system_cpu_usage_percent', 'system_memory_usage_percent', 'system_disk_usage_percent', 'system_disk_free_bytes',
      'pilot_process_up', 'mariadb_connections', 'mariadb_max_connections')
  GROUP BY metric, service
),
requests AS (
  SELECT count() AS total, countIf(toUInt16OrZero(attributes['status']) >= 500) AS failed
  FROM logs
  WHERE resource_id LIKE 'cargo:%' AND source = 'nginx-access.log' AND ts > now() - INTERVAL 1 HOUR
)
SELECT
  (SELECT if(dateDiff('second', maxOrNull(at), now()) < 120, 1, 0) FROM latest) AS reporting,
  (SELECT maxIf(value, metric = 'system_cpu_usage_percent') FROM latest) AS cpu_percent,
  (SELECT maxIf(value, metric = 'system_memory_usage_percent') FROM latest) AS memory_percent,
  (SELECT maxIf(value, metric = 'system_disk_usage_percent') FROM latest) AS disk_percent,
  (SELECT maxIf(value, metric = 'system_disk_free_bytes') / 1e9 FROM latest) AS disk_free_gb,
  (SELECT sumIf(value, metric = 'pilot_process_up') FROM latest) AS processes_up,
  (SELECT countIf(metric = 'pilot_process_up') FROM latest) AS processes,
  (SELECT 100 * maxIf(value, metric = 'mariadb_connections') / nullIf(maxIf(value, metric = 'mariadb_max_connections'), 0) FROM latest) AS db_connections_percent,
  (SELECT total FROM requests) AS requests_1h,
  (SELECT failed FROM requests) AS server_errors_1h,
  (SELECT count() FROM logs WHERE resource_id LIKE 'cargo:%' AND level IN ('error', 'alert', 'critical', 'emergency') AND ts > now() - INTERVAL 1 HOUR) AS error_logs_1h
