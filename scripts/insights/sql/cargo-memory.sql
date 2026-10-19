SELECT toStartOfMinute(ts) AS time, replaceOne(replaceOne(metric, 'system_memory_', ''), '_bytes', '') AS series, avg(value) / 1073741824 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric IN ('system_memory_total_bytes', 'system_memory_used_bytes', 'system_memory_cached_bytes', 'system_memory_swap_used_bytes')
  AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
