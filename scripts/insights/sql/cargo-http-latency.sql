SELECT time, series, value FROM (
  SELECT toStartOfInterval(ts, INTERVAL 5 MINUTE) AS time,
    1000 * avg(toFloat64OrZero(attributes['response_time'])) AS average,
    1000 * quantile(0.95)(toFloat64OrZero(attributes['response_time'])) AS p95
  FROM logs
  WHERE resource_id LIKE 'cargo:%' AND source = 'nginx-access.log' AND attributes['response_time'] != '' AND ts > now() - INTERVAL 24 HOUR
  GROUP BY time
)
ARRAY JOIN ['average', 'p95'] AS series, [average, p95] AS value
