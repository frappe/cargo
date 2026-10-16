WITH
latest AS (
  SELECT labels['cluster'] AS cluster, labels['machine'] AS machine, any(labels['role']) AS role, metric, argMax(value, ts) AS value, max(ts) AS at
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE
    AND metric IN ('host_memory_available_bytes', 'host_memory_total_bytes', 'host_load5', 'host_logical_cpus', 'host_filesystem_used_ratio')
    AND (metric != 'host_filesystem_used_ratio' OR labels['mountpoint'] = '/')
  GROUP BY cluster, machine, metric
),
cpu AS (
  SELECT machine, 100 * (1 - (argMax(idle, ts) - argMin(idle, ts)) / nullIf(argMax(total, ts) - argMin(total, ts), 0)) AS cpu
  FROM (
    SELECT labels['machine'] AS machine, ts, sumIf(value, labels['mode'] IN ('idle', 'io_wait')) AS idle, sum(value) AS total
    FROM samples WHERE metric = 'host_cpu_seconds_total' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY machine, ts
  )
  GROUP BY machine
),
network AS (
  SELECT machine,
    sumIf(rate, metric = 'host_network_receive_bytes_total') AS received,
    sumIf(rate, metric = 'host_network_transmit_bytes_total') AS sent
  FROM (
    SELECT labels['machine'] AS machine, metric, labels['device'] AS device,
      greatest(argMax(value, ts) - argMin(value, ts), 0) / nullIf(dateDiff('second', min(ts), max(ts)), 0) / 1048576 AS rate
    FROM samples WHERE metric IN ('host_network_receive_bytes_total', 'host_network_transmit_bytes_total') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY machine, metric, device
  )
  GROUP BY machine
)
SELECT l.cluster AS cluster, l.machine AS machine, any(l.role) AS role, max(l.at) AS last_seen,
  any(c.cpu) AS cpu_percent,
  100 * (1 - maxIf(l.value, l.metric = 'host_memory_available_bytes') / nullIf(maxIf(l.value, l.metric = 'host_memory_total_bytes'), 0)) AS memory_percent,
  maxIf(l.value, l.metric = 'host_load5') / nullIf(maxIf(l.value, l.metric = 'host_logical_cpus'), 0) AS load_per_core,
  100 * maxIf(l.value, l.metric = 'host_filesystem_used_ratio') AS root_disk_percent,
  any(n.received) AS received_mb, any(n.sent) AS sent_mb
FROM latest AS l
LEFT JOIN cpu AS c ON c.machine = l.machine
LEFT JOIN network AS n ON n.machine = l.machine
GROUP BY cluster, machine
ORDER BY machine
