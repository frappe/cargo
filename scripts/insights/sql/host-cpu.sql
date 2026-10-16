WITH totals AS (
  SELECT labels['machine'] AS machine, ts, sumIf(value, labels['mode'] IN ('idle', 'io_wait')) AS idle, sum(value) AS total
  FROM samples
  WHERE metric = 'host_cpu_seconds_total' AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  GROUP BY machine, ts
),
deltas AS (
  SELECT machine, ts, idle - lagInFrame(idle) OVER w AS idle_delta, total - lagInFrame(total) OVER w AS total_delta, row_number() OVER w AS position
  FROM totals
  WINDOW w AS (PARTITION BY machine ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, machine AS series, 100 * (1 - idle_delta / total_delta) AS value
FROM deltas
WHERE position > 1 AND total_delta > 0 AND ts > now() - INTERVAL 24 HOUR
