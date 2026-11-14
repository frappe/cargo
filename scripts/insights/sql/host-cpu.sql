WITH totals AS (
  SELECT resource_id AS machine, ts, sumIf(value, labels['mode'] IN ('idle', 'iowait')) AS idle, sum(value) AS total
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND metric = 'system_cpu_seconds_total' AND labels['mode'] NOT IN ('guest', 'guest_nice') AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
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
