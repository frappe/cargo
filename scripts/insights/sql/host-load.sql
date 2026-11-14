SELECT time, series, load / cpus AS value
FROM (
  SELECT ts AS time, resource_id AS series,
    maxIf(value, metric = 'system_load_average') AS load, maxIf(value, metric = 'system_cpu_count') AS cpus
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND (metric = 'system_cpu_count' OR (metric = 'system_load_average' AND labels['window'] = '5m')) AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time, series
)
WHERE cpus > 0
