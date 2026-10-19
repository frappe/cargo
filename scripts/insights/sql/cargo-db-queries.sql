WITH deltas AS (
  SELECT ts, labels['kind'] AS kind, value, lagInFrame(value) OVER w AS previous, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric = 'mariadb_queries_total' AND ts > now() - INTERVAL 24 HOUR - INTERVAL 10 MINUTE
  WINDOW w AS (PARTITION BY labels['kind'] ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT toStartOfMinute(ts) AS time, kind AS series, avg(if(value < previous, value, value - previous) / dateDiff('millisecond', previous_ts, ts) * 1000) AS value
FROM deltas
WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
