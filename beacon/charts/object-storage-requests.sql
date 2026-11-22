-- title: S3 Requests
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: req/s
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- All S3 requests and the 5xx answers. 403 and 404 are normal S3 answers, so the object-storage-errors rule counts only 5xx.
SELECT ts, series, sum(rate) AS value
FROM (
    SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, machine, labels, sum(increase) / nullIf(sum(seconds), 0) AS rate
    FROM (
        SELECT s.ts AS sample_ts, s.resource_id AS machine, s.labels AS labels,
               if(s.metric = 'garage_api_s3_request_counter', 'Requests', '5xx') AS series,
               s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase,
               dateDiff('millisecond', lagInFrame(s.ts, 1, s.ts) OVER w, s.ts) / 1000 AS seconds
        FROM datum.samples AS s
        WHERE (s.metric = 'garage_api_s3_request_counter' OR (s.metric = 'garage_api_s3_error_counter' AND startsWith(s.labels['status_code'], '5')))
            AND s.resource_id IN (
                SELECT labels['machine'] FROM datum.samples
                WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
                    AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
            )
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.resource_id, s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY ts, series, machine, labels
)
GROUP BY ts, series
ORDER BY ts, series
