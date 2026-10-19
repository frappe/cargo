SELECT toStartOfInterval(ts, INTERVAL 15 MINUTE) AS time, concat(service, ' ', level) AS series, count() AS value
FROM logs
WHERE resource_id LIKE 'cargo:%' AND level NOT IN ('info', 'debug') AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
