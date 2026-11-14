SELECT toStartOfInterval(ts, INTERVAL 10 MINUTE) AS time, 'used' AS series,
  100 * (1 - minIf(value, metric = 'system_filesystem_available_bytes') / maxIf(value, metric = 'system_filesystem_size_bytes')) AS value
FROM samples
WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
  AND metric IN ('system_filesystem_available_bytes', 'system_filesystem_size_bytes') AND labels['mountpoint'] = '/'
  AND ts > now() - INTERVAL 7 DAY
GROUP BY time
