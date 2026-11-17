-- title: Partitions With Quorum
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 15
-- The share of Garage partitions that have a write quorum, on the Cargo machine that sees the fewest. Below 100% some objects cannot be written.
SELECT ts, 'With quorum' AS series, min(share) AS value
FROM (
    SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.resource_id AS machine,
           100 * minIf(s.value, s.metric = 'garage_cluster_partitions_quorum') / nullIf(maxIf(s.value, s.metric = 'garage_cluster_partitions'), 0) AS share
    FROM datum.samples AS s
    WHERE s.metric IN ('garage_cluster_partitions', 'garage_cluster_partitions_quorum')
        AND s.resource_id IN (
            SELECT labels['machine'] FROM datum.samples
            WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
                AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
        )
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY ts, machine
)
GROUP BY ts, series
ORDER BY ts, series
