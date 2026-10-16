SELECT time, series, load / cpus AS value
FROM (
  SELECT ts AS time, labels['machine'] AS series,
    maxIf(value, metric = 'host_load5') AS load, maxIf(value, metric = 'host_logical_cpus') AS cpus
  FROM samples
  WHERE metric IN ('host_load5', 'host_logical_cpus') AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time, series
)
WHERE cpus > 0
