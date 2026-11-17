-- title: Object storage redundancy is lost
-- description: A storage node is down or blocks failed to copy. The cluster still serves data but can lose it.
-- severity: Medium
-- resource_type: Cargo Server
-- charts: object-storage-nodes, object-storage-resync
-- interval: 300
-- pending_for: 600
-- resolve_after: 600
-- The cluster still serves data, but a storage node is down or blocks failed to copy.
SELECT 'cargo' AS resource_id, kind AS alert_key, value, detail
FROM (
    SELECT 'storage-nodes' AS kind, toFloat64(max(total) - min(healthy)) AS value,
           concat(toString(value), ' storage nodes are not OK') AS detail
    FROM (
        SELECT resource_id,
               argMaxIf(value, ts, metric = 'garage_cluster_storage_nodes') AS total,
               argMaxIf(value, ts, metric = 'garage_cluster_storage_nodes_ok') AS healthy
        FROM datum.samples_recent
        WHERE metric IN ('garage_cluster_storage_nodes', 'garage_cluster_storage_nodes_ok') AND ts > now() - INTERVAL 10 MINUTE
          AND resource_id IN (
              SELECT labels['machine'] FROM datum.samples_recent
              WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] IN ('storage', 'gateway')
                AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE
          )
        GROUP BY resource_id
    )
    HAVING count() > 0
    UNION ALL
    SELECT 'resync-errors', toFloat64(max(errored)), concat(toString(max(errored)), ' blocks failed to resync')
    FROM (
        SELECT resource_id, argMax(value, ts) AS errored
        FROM datum.samples_recent
        WHERE metric = 'garage_block_resync_errored_blocks' AND ts > now() - INTERVAL 10 MINUTE
          AND resource_id IN (
              SELECT labels['machine'] FROM datum.samples_recent
              WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] IN ('storage', 'gateway')
                AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE
          )
        GROUP BY resource_id
    )
    HAVING count() > 0
)
WHERE value > 0
