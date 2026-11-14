-- title: Object storage disk almost full
-- severity: High
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-disk-full.md
-- interval: 300
-- pending_for: 600
-- resolve_after: 900
SELECT 'cargo' AS resource_id, concat(machine, ' ', volume) AS alert_key, 100 * avail / total AS value,
       concat('the ', volume, ' disk of ', machine, ' has ', toString(round(value, 1)), '% free') AS detail
FROM (
    SELECT resource_id AS machine, labels['volume'] AS volume,
           argMaxIf(value, ts, metric = 'garage_local_disk_avail') AS avail,
           argMaxIf(value, ts, metric = 'garage_local_disk_total') AS total
    FROM datum.samples_recent
    WHERE metric IN ('garage_local_disk_avail', 'garage_local_disk_total') AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY machine, volume
)
WHERE total > 0 AND avail < 0.1 * total
