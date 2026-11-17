-- title: Metadata Backup Age
-- resource_type: Cargo Server
-- section: Object Storage
-- unit: h
-- style: Line
-- is_key_chart: 0
-- sort_order: 40
-- The age of the newest metadata backup of each cluster, as the object-storage-backup-stale rule reads it.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['cluster'] AS series, max(s.value) / 3600 AS value
FROM datum.samples AS s
WHERE s.metric = 'cargo_object_storage_metadata_backup_age_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
