WITH
health AS (
  SELECT labels['machine'] AS machine, metric, argMax(value, ts) AS value
  FROM samples
  WHERE metric IN ('garage_cluster_healthy', 'garage_cluster_storage_nodes_ok', 'garage_cluster_storage_nodes', 'garage_cluster_connected_nodes', 'garage_local_disk_avail', 'garage_local_disk_total')
    AND ts > now() - INTERVAL 15 MINUTE AND (metric NOT LIKE 'garage_local_disk_%' OR labels['volume'] = 'data')
  GROUP BY machine, metric
),
host AS (
  SELECT labels['machine'] AS machine, ts,
    sumIf(value, metric = 'host_cpu_seconds_total' AND labels['mode'] IN ('idle', 'io_wait')) AS idle,
    sumIf(value, metric = 'host_cpu_seconds_total') AS total,
    maxIf(value, metric = 'host_memory_available_bytes') AS available,
    maxIf(value, metric = 'host_memory_total_bytes') AS memory
  FROM samples
  WHERE metric IN ('host_cpu_seconds_total', 'host_memory_available_bytes', 'host_memory_total_bytes') AND ts > now() - INTERVAL 5 MINUTE
  GROUP BY machine, ts
),
counters AS (
  SELECT metric, labels,
    greatest(argMax(value, ts) - argMin(value, ts), 0) AS delta,
    dateDiff('second', min(ts), max(ts)) AS seconds
  FROM samples
  WHERE metric IN ('garage_api_s3_request_counter', 'garage_api_s3_error_counter', 'garage_api_s3_request_duration_sum', 'garage_api_s3_request_duration_count')
    AND ts > now() - INTERVAL 16 MINUTE
  GROUP BY metric, labels
)
SELECT
  (SELECT minIfOrNull(value, metric = 'garage_cluster_healthy') FROM health) AS healthy,
  (SELECT minIfOrNull(value, metric = 'garage_cluster_storage_nodes_ok') FROM health) AS storage_nodes_ok,
  (SELECT minIfOrNull(value, metric = 'garage_cluster_connected_nodes') FROM health) AS connected_nodes,
  (SELECT uniqExact(machine) FROM health) AS reporting_nodes,
  (SELECT sumIf(delta / nullIf(seconds, 0), metric = 'garage_api_s3_request_counter') FROM counters) AS requests_per_second,
  (SELECT sumIf(delta / nullIf(seconds, 0), metric = 'garage_api_s3_error_counter') FROM counters) AS errors_per_second,
  (SELECT 1000 * sumIf(delta, metric = 'garage_api_s3_request_duration_sum') / nullIf(sumIf(delta, metric = 'garage_api_s3_request_duration_count'), 0) FROM counters) AS latency_ms,
  (SELECT maxOrNull(100 * (1 - avail / total)) FROM (
    SELECT machine, maxIf(value, metric = 'garage_local_disk_avail') AS avail, maxIf(value, metric = 'garage_local_disk_total') AS total
    FROM health WHERE metric LIKE 'garage_local_disk_%' GROUP BY machine HAVING total > 0
  )) AS max_disk_used,
  (SELECT maxOrNull(cpu) FROM (
    SELECT machine, 100 * (1 - (argMax(idle, ts) - argMin(idle, ts)) / nullIf(argMax(total, ts) - argMin(total, ts), 0)) AS cpu FROM host GROUP BY machine
  )) AS max_cpu,
  (SELECT maxOrNull(used) FROM (
    SELECT machine, 100 * (1 - argMax(available, ts) / nullIf(argMax(memory, ts), 0)) AS used FROM host GROUP BY machine
  )) AS max_memory
