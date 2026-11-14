-- title: Object storage unavailable
-- severity: Critical
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-unavailable.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- Garage is available while every partition has a write quorum. A node that reported in the last hour and stopped counts as down,
-- so the alert resolves only on a fresh report that the cluster is available.
SELECT 'cargo' AS resource_id, '' AS alert_key, countIf(is_up) AS value,
       concat('Garage is unavailable or silent on ', toString(countIf(NOT is_up)), ' of ', toString(count()), ' nodes') AS detail
FROM (
    SELECT resource_id, argMax(value, ts) = 1 AND max(ts) > now() - INTERVAL 5 MINUTE AS is_up
    FROM datum.samples_recent
    WHERE metric = 'garage_cluster_available' AND ts > now() - INTERVAL 1 HOUR
    GROUP BY resource_id
)
HAVING count() > 0 AND value = 0
