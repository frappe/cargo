SELECT time, series, 100 * (1 - available / total) AS value
FROM (
  SELECT ts AS time, resource_id AS series,
    maxIf(value, metric = 'system_memory_available_bytes') AS available, maxIf(value, metric = 'system_memory_total_bytes') AS total
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND metric IN ('system_memory_available_bytes', 'system_memory_total_bytes') AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time, series
)
WHERE total > 0
