WITH deltas AS (
  SELECT ts, metric, labels, value, lagInFrame(value) OVER w AS previous, row_number() OVER w AS position
  FROM samples
  WHERE metric IN ('garage_api_s3_request_duration_sum', 'garage_api_s3_request_duration_count') AND ts > now() - INTERVAL 24 HOUR - INTERVAL 15 MINUTE
  WINDOW w AS (PARTITION BY metric, toString(labels) ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT ts AS time, labels['api_endpoint'] AS series,
  1000 * sumIf(if(value < previous, value, value - previous), metric = 'garage_api_s3_request_duration_sum')
    / nullIf(sumIf(if(value < previous, value, value - previous), metric = 'garage_api_s3_request_duration_count'), 0) AS value
FROM deltas
WHERE position > 1 AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
HAVING value IS NOT NULL
