WITH
machines AS (
  SELECT labels['machine'] AS machine, argMax(labels['cluster'], ts) AS cluster, argMax(labels['role'], ts) AS role
  FROM samples
  WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY
  GROUP BY machine
)
SELECT
  m.cluster AS cluster,
  m.machine AS machine,
  m.role AS role,
  s.labels['volume'] AS volume,
  argMaxIf(s.value, s.ts, s.metric = 'garage_local_disk_total') / 1e9 AS total_gb,
  argMaxIf(s.value, s.ts, s.metric = 'garage_local_disk_avail') / 1e9 AS free_gb,
  100 * (1 - argMaxIf(s.value, s.ts, s.metric = 'garage_local_disk_avail') / argMaxIf(s.value, s.ts, s.metric = 'garage_local_disk_total')) AS used_percent
FROM samples AS s
INNER JOIN machines AS m ON m.machine = s.resource_id
WHERE s.metric IN ('garage_local_disk_avail', 'garage_local_disk_total') AND s.ts > now() - INTERVAL 15 MINUTE
GROUP BY cluster, machine, role, volume
