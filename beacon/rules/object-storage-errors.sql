-- title: Object storage errors
-- severity: High
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-errors.md
-- interval: 60
-- pending_for: 300
-- resolve_after: 600
-- Only 5xx answers count. 403 and 404 are normal S3 answers.
SELECT 'cargo' AS resource_id, '' AS alert_key, errors AS value,
       concat(toString(errors), ' of ', toString(requests), ' S3 requests failed with 5xx in 10 min') AS detail
FROM (
    SELECT (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_error_counter', window = 600)
            WHERE startsWith(labels['status_code'], '5')) AS errors,
           (SELECT sum(increase) FROM datum.counter_increase(metric = 'garage_api_s3_request_counter', window = 600)) AS requests
)
WHERE errors >= 10 AND errors > 0.01 * requests
