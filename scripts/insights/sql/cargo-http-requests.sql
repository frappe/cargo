SELECT toStartOfInterval(ts, INTERVAL 5 MINUTE) AS time, concat(left(attributes['status'], 1), 'xx') AS series, count() AS value
FROM logs
WHERE resource_id LIKE 'cargo:%' AND source = 'nginx-access.log' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
