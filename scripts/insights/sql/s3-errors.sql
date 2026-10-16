WITH deltas AS (
  SELECT ts, metric, labels, value, lagInFrame(value) OVER w AS previous, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
  FROM samples
  WHERE metric = 'garage_api_s3_error_counter' AND ts > now() - INTERVAL 24 HOUR - INTERVAL 15 MINUTE
  WINDOW w AS (PARTITION BY metric, toString(labels) ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, concat(labels['status_code'], ' ', labels['api_endpoint']) AS series,
  sum(if(value < previous, value, value - previous) * 1000 / dateDiff('millisecond', previous_ts, ts)) AS value
FROM deltas
WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
