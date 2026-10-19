SELECT ts AS time, if(metric = 'mariadb_connections', 'connected', 'max') AS series, value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric IN ('mariadb_connections', 'mariadb_max_connections') AND ts > now() - INTERVAL 24 HOUR
