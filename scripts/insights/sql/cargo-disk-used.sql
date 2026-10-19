SELECT toStartOfInterval(ts, INTERVAL 10 MINUTE) AS time, 'used' AS series, max(value) AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'system_disk_usage_percent' AND ts > now() - INTERVAL 7 DAY
GROUP BY time
