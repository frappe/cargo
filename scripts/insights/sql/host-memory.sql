SELECT time, series, 100 * (1 - available / total) AS value
FROM (
  SELECT ts AS time, labels['machine'] AS series,
    maxIf(value, metric = 'host_memory_available_bytes') AS available, maxIf(value, metric = 'host_memory_total_bytes') AS total
  FROM samples
  WHERE metric IN ('host_memory_available_bytes', 'host_memory_total_bytes') AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time, series
)
WHERE total > 0
