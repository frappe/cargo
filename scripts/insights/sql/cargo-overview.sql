WITH
latest AS (
  SELECT metric, labels['service'] AS service, argMax(value, ts) AS value, max(ts) AS at
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND ts > now() - INTERVAL 5 MINUTE
    AND metric IN ('system_memory_total_bytes', 'system_memory_available_bytes', 'system_filesystem_size_bytes', 'system_filesystem_available_bytes',
      'pilot_process_up', 'mariadb_connections', 'mariadb_max_connections')
    AND (metric NOT LIKE 'system_filesystem_%' OR labels['mountpoint'] = '/')
  GROUP BY metric, service
),
cpu AS (
  SELECT 100 * (1 - (argMax(idle, ts) - argMin(idle, ts)) / nullIf(argMax(total, ts) - argMin(total, ts), 0)) AS busy
  FROM (
    SELECT ts, sumIf(value, labels['mode'] IN ('idle', 'iowait')) AS idle, sum(value) AS total
    FROM samples
    WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
      AND metric = 'system_cpu_seconds_total' AND labels['mode'] NOT IN ('guest', 'guest_nice') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY ts
  )
)
SELECT
  (SELECT if(dateDiff('second', maxOrNull(at), now()) < 120, 1, 0) FROM latest) AS reporting,
  (SELECT busy FROM cpu) AS cpu_percent,
  (SELECT 100 * (1 - maxIf(value, metric = 'system_memory_available_bytes') / nullIf(maxIf(value, metric = 'system_memory_total_bytes'), 0)) FROM latest) AS memory_percent,
  (SELECT 100 * (1 - maxIf(value, metric = 'system_filesystem_available_bytes') / nullIf(maxIf(value, metric = 'system_filesystem_size_bytes'), 0)) FROM latest) AS disk_percent,
  (SELECT maxIf(value, metric = 'system_filesystem_available_bytes') / 1e9 FROM latest) AS disk_free_gb,
  (SELECT sumIf(value, metric = 'pilot_process_up') FROM latest) AS processes_up,
  (SELECT countIf(metric = 'pilot_process_up') FROM latest) AS processes,
  (SELECT 100 * maxIf(value, metric = 'mariadb_connections') / nullIf(maxIf(value, metric = 'mariadb_max_connections'), 0) FROM latest) AS db_connections_percent,
  (SELECT count() FROM logs WHERE (resource_id = 'cargo' OR resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)) AND level IN ('error', 'alert', 'critical', 'emergency') AND ts > now() - INTERVAL 1 HOUR) AS error_logs_1h
