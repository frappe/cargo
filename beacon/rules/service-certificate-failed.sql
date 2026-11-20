-- title: Service certificate could not be issued
-- description: Cargo failed to issue or renew a Let's Encrypt certificate. nginx keeps the old one until it expires.
-- severity: Critical
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/service-certificate.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 300
-- Cargo sends the status of each Service Certificate every minute. The alert stays while the newest status is Failed, and resolves once an issue succeeds.
SELECT 'cargo' AS resource_id, certificate AS alert_key, days_left AS value,
       concat(certificate, ' certificate failed to issue, ', toString(days_left), ' days left on the current one') AS detail
FROM (
    SELECT labels['certificate'] AS certificate, argMax(labels['status'], ts) AS status, argMax(value, ts) AS days_left
    FROM datum.samples_recent
    WHERE metric = 'cargo_service_certificate' AND resource_id = 'cargo' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY certificate
)
WHERE status = 'Failed'
