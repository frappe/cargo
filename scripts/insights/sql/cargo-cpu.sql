WITH totals AS (
  SELECT ts, sumIf(value, labels['mode'] IN ('idle', 'iowait')) AS idle, sumIf(value, labels['mode'] = 'iowait') AS iowait, sum(value) AS total
  FROM samples
  WHERE resource_id IN (SELECT labels['machine'] FROM samples WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] = 'cargo' AND ts > now() - INTERVAL 7 DAY)
    AND metric = 'system_cpu_seconds_total' AND labels['mode'] NOT IN ('guest', 'guest_nice')
    AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  GROUP BY ts
),
deltas AS (
  SELECT ts, idle - lagInFrame(idle) OVER w AS idle_delta, iowait - lagInFrame(iowait) OVER w AS iowait_delta,
    total - lagInFrame(total) OVER w AS total_delta, row_number() OVER w AS position
  FROM totals
  WINDOW w AS (ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, series, value
FROM deltas
ARRAY JOIN ['busy', 'iowait'] AS series, [100 * (1 - idle_delta / total_delta), 100 * iowait_delta / total_delta] AS value
WHERE position > 1 AND total_delta > 0 AND ts > now() - INTERVAL 24 HOUR
