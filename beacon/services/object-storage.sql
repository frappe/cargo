-- title: Object Storage
-- description: S3 requests to object storage succeed.
-- scope: Region
-- slo_target: 99.9
-- sort_order: 4
-- A 5xx answer is bad. 403 and 404 are normal S3 answers, so they are good. Each Garage node that serves the S3 API is one reporter.
-- The window is 3 minutes, so a counter that Vector sends once a minute has the 2 samples that an increase needs.
SELECT r.resource_id AS resource_id,
       toInt64(greatest(r.requests - coalesce(e.errors, 0), 0)) AS good,
       toInt64(r.requests) AS valid,
       t.last_ts AS last_ts
FROM (
    SELECT resource_id, sum(increase) AS requests
    FROM datum.counter_increase(metric = 'garage_api_s3_request_counter', window = 180)
    GROUP BY resource_id
) AS r
LEFT JOIN (
    SELECT resource_id, sum(increase) AS errors
    FROM datum.counter_increase(metric = 'garage_api_s3_error_counter', window = 180)
    WHERE startsWith(labels['status_code'], '5')
    GROUP BY resource_id
) AS e ON e.resource_id = r.resource_id
INNER JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'garage_api_s3_request_counter' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY resource_id
) AS t ON t.resource_id = r.resource_id
