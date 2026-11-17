-- title: Block Resync
-- resource_type: Cargo Server
-- section: Object Storage
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 50
-- The blocks that wait to be copied between nodes, and the blocks that failed to copy, summed over the Cargo machines. The object-storage-redundancy rule reads the failed blocks.
SELECT ts, series, sum(blocks) AS value
FROM (
    SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.resource_id AS machine,
           if(s.metric = 'garage_block_resync_queue_length', 'Waiting', 'Failed') AS series, max(s.value) AS blocks
    FROM datum.samples AS s
    WHERE s.metric IN ('garage_block_resync_queue_length', 'garage_block_resync_errored_blocks')
        AND s.resource_id IN (
            SELECT labels['machine'] FROM datum.samples
            WHERE metric = 'cargo_machine' AND resource_id = {resource_id:String} AND labels['role'] IN ('storage', 'gateway')
                AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
        )
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY ts, machine, series
)
GROUP BY ts, series
ORDER BY ts, series
