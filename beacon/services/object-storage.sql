-- title: Object Storage
-- description: S3 requests to object storage succeed.
-- scope: Region
-- slo_target: 99.9
-- sort_order: 4
-- A 5xx answer is bad. 403 and 404 are normal S3 answers, so they are good. The Garage nodes send with their own resource IDs, but the
-- resource list has one resource for Cargo, so the query reports the object storage of the region as the one reporter 'cargo'.
-- Only the nodes that Cargo lists in cargo_machine count, because any sender can send these metrics.
-- The window is 3 minutes, so a counter that the Datum agent scrapes each minute has the 2 samples that an increase needs.
WITH (SELECT groupUniqArray(labels['machine']) FROM datum.samples_recent
      WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] IN ('storage', 'gateway')
        AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE) AS nodes
SELECT 'cargo' AS resource_id, toInt64(greatest(requests - errors, 0)) AS good, toInt64(requests) AS valid, last_ts
FROM (
    SELECT (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_request_counter', window = 180) WHERE has(nodes, resource_id)) AS requests,
           (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_error_counter', window = 180)
            WHERE has(nodes, resource_id) AND startsWith(labels['status_code'], '5')) AS errors,
           (SELECT max(ts) FROM datum.samples_recent
            WHERE metric = 'garage_api_s3_request_counter' AND has(nodes, resource_id) AND ts > now() - INTERVAL 10 MINUTE) AS last_ts
)
WHERE last_ts IS NOT NULL AND last_ts > toDateTime64(0, 3)
