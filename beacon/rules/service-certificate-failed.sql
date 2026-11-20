-- title: Service certificate could not be issued
-- description: Cargo failed to issue or renew a Let's Encrypt certificate. nginx keeps the old one until it expires.
-- severity: Critical
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/service-certificate.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 300
-- Cargo logs each failed issue as an Error Log, which reaches Datum within a minute. A Failed certificate is retried daily, so the 25-hour window keeps the alert open while it keeps failing, and it resolves a day after the last failure.
SELECT 'cargo' AS resource_id, certificate AS alert_key, toFloat64(failures) AS value,
       concat(certificate, ' certificate could not be issued, last at ', toString(last_ts)) AS detail
FROM (
    SELECT extract(message, '^Could not issue the (.+) certificate$') AS certificate, count() AS failures, max(ts) AS last_ts
    FROM datum.logs
    WHERE resource_id = 'cargo' AND product = 'cargo' AND source = 'error_log'
      AND message LIKE 'Could not issue the % certificate' AND ts > now() - INTERVAL 25 HOUR
    GROUP BY certificate
)
