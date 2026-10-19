WITH
latest AS (
  SELECT replaceOne(replaceOne(labels['service'], 'cargo-', ''), '.service', '') AS service, metric, argMax(value, ts) AS value, max(ts) AS at
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric LIKE 'pilot_process_%' AND metric != 'pilot_process_state' AND ts > now() - INTERVAL 5 MINUTE
  GROUP BY service, metric
),
states AS (
  SELECT replaceOne(replaceOne(labels['service'], 'cargo-', ''), '.service', '') AS service, argMax(labels['state'], ts) AS state
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric = 'pilot_process_state' AND ts > now() - INTERVAL 5 MINUTE
  GROUP BY service
),
restarts AS (
  SELECT service, countIf(up = 1 AND previous = 0) AS restarts, countIf(up = 0) AS down_samples
  FROM (
    SELECT replaceOne(replaceOne(labels['service'], 'cargo-', ''), '.service', '') AS service, value AS up,
      lagInFrame(value, 1, 1) OVER (PARTITION BY labels['service'] ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS previous
    FROM samples
    WHERE resource_id LIKE 'cargo:%' AND metric = 'pilot_process_up' AND ts > now() - INTERVAL 24 HOUR
  )
  GROUP BY service
)
SELECT l.service AS service, any(s.state) AS state, max(l.at) AS last_seen,
  maxIf(l.value, l.metric = 'pilot_process_up') AS up,
  maxIf(l.value, l.metric = 'pilot_process_cpu_percent') AS cpu_percent,
  maxIf(l.value, l.metric = 'pilot_process_memory_rss_bytes') / 1048576 AS memory_mb,
  maxIf(l.value, l.metric = 'pilot_process_open_fds') AS open_fds,
  any(r.restarts) AS restarts_24h, any(r.down_samples) * 12 / 60 AS down_minutes_24h
FROM latest AS l
LEFT JOIN states AS s ON s.service = l.service
LEFT JOIN restarts AS r ON r.service = l.service
GROUP BY service
ORDER BY service
