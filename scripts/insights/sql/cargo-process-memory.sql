SELECT toStartOfMinute(ts) AS time, replaceOne(replaceOne(labels['service'], 'cargo-', ''), '.service', '') AS series, avg(value) / 1048576 AS value
FROM samples
WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
  AND metric = 'pilot_process_memory_rss_bytes' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
