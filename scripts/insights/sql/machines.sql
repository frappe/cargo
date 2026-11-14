WITH
machines AS (
  SELECT labels['machine'] AS machine, argMax(labels['cluster'], ts) AS cluster, argMax(labels['role'], ts) AS role
  FROM samples
  WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY
  GROUP BY machine
),
latest AS (
  SELECT resource_id AS machine, metric, argMax(value, ts) AS value, max(ts) AS at
  FROM samples
  WHERE resource_id IN (SELECT machine FROM machines) AND ts > now() - INTERVAL 15 MINUTE
    AND metric IN ('system_memory_available_bytes', 'system_memory_total_bytes', 'system_load_average', 'system_cpu_count',
      'system_filesystem_available_bytes', 'system_filesystem_size_bytes')
    AND (metric != 'system_load_average' OR labels['window'] = '5m')
    AND (metric NOT LIKE 'system_filesystem_%' OR labels['mountpoint'] = '/')
  GROUP BY machine, metric
),
cpu AS (
  SELECT machine, 100 * (1 - (argMax(idle, ts) - argMin(idle, ts)) / nullIf(argMax(total, ts) - argMin(total, ts), 0)) AS cpu
  FROM (
    SELECT resource_id AS machine, ts, sumIf(value, labels['mode'] IN ('idle', 'iowait')) AS idle, sum(value) AS total
    FROM samples
    WHERE resource_id IN (SELECT machine FROM machines) AND metric = 'system_cpu_seconds_total' AND labels['mode'] NOT IN ('guest', 'guest_nice')
      AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY machine, ts
  )
  GROUP BY machine
),
network AS (
  SELECT machine,
    sumIf(rate, metric = 'system_network_receive_bytes_total') AS received,
    sumIf(rate, metric = 'system_network_transmit_bytes_total') AS sent
  FROM (
    SELECT resource_id AS machine, metric, labels['interface'] AS interface,
      greatest(argMax(value, ts) - argMin(value, ts), 0) / nullIf(dateDiff('second', min(ts), max(ts)), 0) / 1048576 AS rate
    FROM samples
    WHERE resource_id IN (SELECT machine FROM machines) AND metric IN ('system_network_receive_bytes_total', 'system_network_transmit_bytes_total')
      AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY machine, metric, interface
  )
  GROUP BY machine
)
SELECT m.cluster AS cluster, m.machine AS machine, m.role AS role, max(l.at) AS last_seen,
  any(c.cpu) AS cpu_percent,
  100 * (1 - maxIf(l.value, l.metric = 'system_memory_available_bytes') / nullIf(maxIf(l.value, l.metric = 'system_memory_total_bytes'), 0)) AS memory_percent,
  maxIf(l.value, l.metric = 'system_load_average') / nullIf(maxIf(l.value, l.metric = 'system_cpu_count'), 0) AS load_per_core,
  100 * (1 - maxIf(l.value, l.metric = 'system_filesystem_available_bytes') / nullIf(maxIf(l.value, l.metric = 'system_filesystem_size_bytes'), 0)) AS root_disk_percent,
  any(n.received) AS received_mb, any(n.sent) AS sent_mb
FROM latest AS l
INNER JOIN machines AS m ON m.machine = l.machine
LEFT JOIN cpu AS c ON c.machine = l.machine
LEFT JOIN network AS n ON n.machine = l.machine
GROUP BY cluster, machine, role
ORDER BY machine
