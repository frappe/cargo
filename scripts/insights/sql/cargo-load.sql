SELECT toStartOfMinute(ts) AS time, labels['window'] AS series, avg(value) AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'system_load_average' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
