-- title: Storage Disk Used
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 30
-- The fullest Garage disk of each machine, data or metadata, as the object-storage-disk-full rule reads them.
SELECT ts, machine AS series, max(used) AS value
FROM (
    SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.resource_id AS machine, s.labels['volume'] AS volume,
           100 * (1 - avgIf(s.value, s.metric = 'garage_local_disk_avail') / nullIf(avgIf(s.value, s.metric = 'garage_local_disk_total'), 0)) AS used
    FROM datum.samples AS s
    WHERE s.metric IN ('garage_local_disk_avail', 'garage_local_disk_total')
        AND s.resource_id IN (
            SELECT labels['machine'] FROM datum.samples
            WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
                AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
        )
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY ts, machine, volume
)
GROUP BY ts, series
ORDER BY ts, series
