-- title: Object storage returns 5xx errors
-- description: More than 1% of S3 requests failed with a 5xx status in 10 minutes. Uploads and downloads fail.
-- severity: High
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-errors.md
-- charts: object-storage-requests, object-storage-rpc
-- interval: 60
-- pending_for: 300
-- resolve_after: 600
-- Only 5xx answers count. 403 and 404 are normal S3 answers.
WITH (SELECT groupUniqArray(labels['machine']) FROM datum.samples_recent
      WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] IN ('storage', 'gateway')
        AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE) AS nodes
SELECT 'cargo' AS resource_id, '' AS alert_key, errors AS value,
       concat(toString(errors), ' of ', toString(requests), ' S3 requests failed with 5xx in 10 min (limit 1%)') AS detail
FROM (
    SELECT (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_error_counter', window = 600)
            WHERE has(nodes, resource_id) AND startsWith(labels['status_code'], '5')) AS errors,
           (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_request_counter', window = 600) WHERE has(nodes, resource_id)) AS requests
)
WHERE errors >= 10 AND errors > 0.01 * requests
