SELECT toStartOfMinute(ts) AS time, if(metric = 'mariadb_connections', 'connected', 'max') AS series, max(value) AS value
FROM samples
WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
  AND metric IN ('mariadb_connections', 'mariadb_max_connections') AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
