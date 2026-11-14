WITH totals AS (
  SELECT metric, ts, sum(value) AS value
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND metric IN ('system_disk_read_bytes_total', 'system_disk_written_bytes_total') AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  GROUP BY metric, ts
),
deltas AS (
  SELECT metric, ts, value, lagInFrame(value) OVER w AS previous, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
  FROM totals
  WINDOW w AS (PARTITION BY metric ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, if(metric = 'system_disk_read_bytes_total', 'read', 'write') AS series,
  if(value < previous, value, value - previous) / dateDiff('millisecond', previous_ts, ts) * 1000 / 1048576 AS value
FROM deltas
WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
