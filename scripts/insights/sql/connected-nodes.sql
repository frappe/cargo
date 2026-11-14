SELECT ts AS time, resource_id AS series, max(value) AS value
FROM samples
WHERE metric = 'garage_cluster_connected_nodes' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
