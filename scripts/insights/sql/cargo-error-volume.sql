SELECT toStartOfInterval(ts, INTERVAL 15 MINUTE) AS time, concat(service, ' ', level) AS series, count() AS value
FROM logs
WHERE (resource_id = 'cargo' OR resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY))
  AND level NOT IN ('info', 'debug') AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
