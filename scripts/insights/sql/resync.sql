SELECT ts AS time, concat(labels['machine'], if(metric = 'garage_block_resync_queue_length', ' queue', ' errored')) AS series, max(value) AS value
FROM samples
WHERE metric IN ('garage_block_resync_queue_length', 'garage_block_resync_errored_blocks') AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
