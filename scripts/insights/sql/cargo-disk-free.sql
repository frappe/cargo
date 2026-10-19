SELECT toStartOfInterval(ts, INTERVAL 10 MINUTE) AS time, if(metric = 'system_disk_free_bytes', 'free', 'total') AS series, min(value) / 1e9 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric IN ('system_disk_free_bytes', 'system_disk_total_bytes') AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
