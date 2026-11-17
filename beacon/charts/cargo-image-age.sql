-- title: Newest Image Age
-- resource_type: Cargo Server
-- section: Images
-- unit: h
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- The age of the oldest "newest usable image", by image type, as the cargo-images-stale rule reads it. New sites need a fresh image.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['image_type'] AS series, max(s.value) / 3600 AS value
FROM datum.samples AS s
WHERE s.metric = 'cargo_pilot_image_newest_completed_age_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
