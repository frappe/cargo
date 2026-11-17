-- title: S3 Mean Latency
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: ms
-- style: Line
-- is_key_chart: 0
-- sort_order: 25
-- The mean time of the S3 requests that each Cargo machine answered. A mean hides a few slow requests, so compare it with the request rate.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, machine AS series,
       1000 * sumIf(increase, metric = 'garage_api_s3_request_duration_sum') / nullIf(sumIf(increase, metric = 'garage_api_s3_request_duration_count'), 0) AS value
FROM (
    SELECT s.ts AS sample_ts, s.resource_id AS machine, s.metric AS metric,
           s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase
    FROM datum.samples AS s
    WHERE s.metric IN ('garage_api_s3_request_duration_sum', 'garage_api_s3_request_duration_count')
        AND s.resource_id IN (
            SELECT labels['machine'] FROM datum.samples
            WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
                AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
        )
        AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.resource_id, s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
GROUP BY ts, series
ORDER BY ts, series
