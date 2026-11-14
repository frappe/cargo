SELECT toStartOfMinute(ts) AS time, labels['state'] AS series, max(value) AS value
FROM samples
WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
  AND metric = 'pilot_sites' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
