-- title: Cargo errors
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- Cargo sends its Error Logs to Datum each minute. One alert for each error fingerprint that repeats. A PermissionError is a caller without access, not a Cargo failure.
SELECT 'cargo' AS resource_id, fingerprint AS alert_key, toFloat64(errors) AS value,
       concat(toString(errors), ' x ', first_message, ' in Cargo in 15 min') AS detail
FROM (
    SELECT if(attributes['fingerprint'] = '', left(message, 200), attributes['fingerprint']) AS fingerprint,
           count() AS errors, any(message) AS first_message
    FROM datum.logs
    WHERE resource_id = 'cargo' AND product = 'cargo' AND source = 'error_log' AND attributes['unhandled'] = '1'
      AND attributes['exception'] != 'frappe.exceptions.PermissionError'
      AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY fingerprint
)
WHERE errors >= 5
