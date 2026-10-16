WITH totals AS (
  SELECT labels['machine'] AS machine, metric, ts, sum(value) AS value
  FROM samples
  WHERE metric IN ('host_disk_read_bytes_total', 'host_disk_written_bytes_total') AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  GROUP BY machine, metric, ts
),
deltas AS (
  SELECT machine, metric, ts, value, lagInFrame(value) OVER w AS previous, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
  FROM totals
  WINDOW w AS (PARTITION BY machine, metric ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, concat(machine, if(metric = 'host_disk_read_bytes_total', ' read', ' write')) AS series,
  if(value < previous, value, value - previous) / dateDiff('millisecond', previous_ts, ts) * 1000 / 1048576 AS value
FROM deltas
WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
