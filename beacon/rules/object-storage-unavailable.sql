-- title: Object storage unavailable
-- description: No Garage node reports that the cluster has a write quorum. Backups, site files and images fail.
-- severity: Critical
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-unavailable.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- Garage is available while every partition has a write quorum. Each storage and gateway node that Cargo lists reports it.
-- The rule fires only when none of these nodes reported available in the last 5 minutes. One node that is down does not fire it. The object-storage-redundancy rule shows that.
-- The alert resolves on the first fresh report from one node that the cluster is available.
SELECT 'cargo' AS resource_id, '' AS alert_key, countIf(is_up) AS value,
       concat('Garage is down or not reporting on ', toString(countIf(NOT is_up)), ' of ', toString(count()), ' nodes') AS detail
FROM (
    SELECT expected.machine AS machine, reports.is_up AS is_up
    FROM (
        SELECT DISTINCT labels['machine'] AS machine FROM datum.samples_recent
        WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] IN ('storage', 'gateway')
          AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE
    ) AS expected
    LEFT JOIN (
        SELECT resource_id, argMax(value, ts) = 1 AND max(ts) > now() - INTERVAL 5 MINUTE AS is_up
        FROM datum.samples_recent
        WHERE metric = 'garage_cluster_available' AND ts > now() - INTERVAL 1 HOUR
        GROUP BY resource_id
    ) AS reports ON reports.resource_id = expected.machine
)
HAVING count() > 0 AND value = 0
