WITH deltas AS (
  SELECT ts, metric, value, lagInFrame(value) OVER w AS previous, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric IN ('mariadb_row_lock_waits_total', 'mariadb_buffer_pool_reads_total', 'mariadb_buffer_pool_read_requests_total')
    AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  WINDOW w AS (PARTITION BY metric ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
),
rates AS (
  SELECT ts, metric, if(value < previous, value, value - previous) AS delta
  FROM deltas WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
)
SELECT toStartOfInterval(ts, INTERVAL 5 MINUTE) AS time, 'row lock waits' AS series, sumIf(delta, metric = 'mariadb_row_lock_waits_total') AS value
FROM rates GROUP BY time
UNION ALL
SELECT toStartOfInterval(ts, INTERVAL 5 MINUTE) AS time, 'buffer pool misses %' AS series,
  100 * sumIf(delta, metric = 'mariadb_buffer_pool_reads_total') / nullIf(sumIf(delta, metric = 'mariadb_buffer_pool_read_requests_total'), 0) AS value
FROM rates GROUP BY time
