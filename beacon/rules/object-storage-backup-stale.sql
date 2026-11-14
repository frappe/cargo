-- title: Object storage backup stale
-- severity: Medium
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-backup-stale.md
-- interval: 900
-- pending_for: 900
-- resolve_after: 3600
-- Garage takes a metadata snapshot every 6 hours and Cargo uploads the newest one every hour. Without a backup from the last day, a lost metadata disk loses the bucket index.
SELECT 'cargo' AS resource_id, cluster AS alert_key, age AS value,
       concat('the newest metadata backup of ', cluster, ' is ', toString(round(age / 3600)), ' h old') AS detail
FROM (
    SELECT labels['cluster'] AS cluster, argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'cargo_object_storage_metadata_backup_age_seconds' AND ts > now() - INTERVAL 3 HOUR
    GROUP BY cluster
)
WHERE age > 86400
