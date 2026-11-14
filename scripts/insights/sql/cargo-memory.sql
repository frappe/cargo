SELECT time, series, value FROM (
  SELECT toStartOfMinute(ts) AS time,
    avgIf(value, metric = 'system_memory_total_bytes') AS total,
    total - avgIf(value, metric = 'system_memory_available_bytes') AS used,
    avgIf(value, metric = 'system_memory_swap_used_bytes') AS swap_used
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND metric IN ('system_memory_total_bytes', 'system_memory_available_bytes', 'system_memory_swap_used_bytes')
    AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time
)
ARRAY JOIN ['total', 'used', 'swap_used'] AS series, [total / 1073741824, used / 1073741824, swap_used / 1073741824] AS value
