SELECT ts AS time, replaceOne(replaceOne(labels['service'], 'cargo-', ''), '.service', '') AS series, value / 1048576 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'pilot_process_memory_rss_bytes' AND ts > now() - INTERVAL 24 HOUR
