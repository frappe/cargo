-- title: Cargo errors
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- Cargo sends its Error Logs to Datum each minute. One alert for each error fingerprint that repeats. A PermissionError is a caller without access, not a Cargo failure.
SELECT 'cargo' AS resource_id, attributes['fingerprint'] AS alert_key, count() AS value,
       concat(toString(value), ' x ', any(message), ' in Cargo in 15 min') AS detail
FROM datum.logs
WHERE product = 'cargo' AND source = 'error_log' AND attributes['unhandled'] = '1'
  AND attributes['exception'] != 'frappe.exceptions.PermissionError'
  AND ts > now() - INTERVAL 15 MINUTE
GROUP BY alert_key
HAVING value >= 5
