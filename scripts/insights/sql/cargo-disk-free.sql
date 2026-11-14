SELECT toStartOfInterval(ts, INTERVAL 10 MINUTE) AS time, if(metric = 'system_filesystem_available_bytes', 'free', 'total') AS series, min(value) / 1e9 AS value
FROM samples
WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
  AND metric IN ('system_filesystem_available_bytes', 'system_filesystem_size_bytes') AND labels['mountpoint'] = '/'
  AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
