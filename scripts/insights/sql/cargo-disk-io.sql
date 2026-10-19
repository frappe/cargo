SELECT toStartOfMinute(ts) AS time, if(metric = 'system_disk_read_bytes_per_second', 'read', 'write') AS series, avg(value) / 1048576 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric IN ('system_disk_read_bytes_per_second', 'system_disk_write_bytes_per_second')
  AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
