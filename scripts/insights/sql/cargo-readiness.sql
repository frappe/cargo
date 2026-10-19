WITH nodes AS (
  SELECT DISTINCT labels['node'] AS node
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_node' AND labels['service'] = 'cargo' AND ts > now() - INTERVAL 7 DAY
)
SELECT toStartOfInterval(ts, INTERVAL 15 MINUTE) AS time, labels['node'] AS series, 100 * avg(value) AS value
FROM samples
WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_ready' AND labels['node'] IN (SELECT node FROM nodes) AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
