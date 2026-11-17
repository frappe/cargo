-- title: Storage Nodes
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: 
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- The storage nodes of the Garage cluster, and those that are up, as each Cargo machine of the cluster sees them.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       if(s.metric = 'garage_cluster_storage_nodes', 'Storage nodes', 'Nodes up') AS series,
       if(s.metric = 'garage_cluster_storage_nodes', max(s.value), min(s.value)) AS value
FROM datum.samples AS s
WHERE s.metric IN ('garage_cluster_storage_nodes', 'garage_cluster_storage_nodes_ok')
    AND s.resource_id IN (
        SELECT labels['machine'] FROM datum.samples
        WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
            AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
    )
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series, s.metric
ORDER BY ts, series
