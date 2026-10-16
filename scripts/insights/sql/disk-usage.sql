SELECT
  labels['cluster'] AS cluster,
  labels['machine'] AS machine,
  labels['role'] AS role,
  labels['volume'] AS volume,
  argMaxIf(value, ts, metric = 'garage_local_disk_total') / 1e9 AS total_gb,
  argMaxIf(value, ts, metric = 'garage_local_disk_avail') / 1e9 AS free_gb,
  100 * (1 - argMaxIf(value, ts, metric = 'garage_local_disk_avail') / argMaxIf(value, ts, metric = 'garage_local_disk_total')) AS used_percent
FROM samples
WHERE metric IN ('garage_local_disk_avail', 'garage_local_disk_total') AND ts > now() - INTERVAL 15 MINUTE
GROUP BY cluster, machine, role, volume
