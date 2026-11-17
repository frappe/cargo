-- title: Failed Builds
-- resource_type: Cargo Server
-- section: Images
-- unit:
-- style: Bar
-- is_key_chart: 0
-- sort_order: 20
-- The images whose newest build attempt failed, by image type, as the cargo-build-failed rule reads them.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, max(images) AS value
FROM (
    SELECT s.ts AS sample_ts, s.labels['image_type'] AS series, uniqIf(s.labels['image'], s.value > 0) AS images
    FROM datum.samples AS s
    WHERE s.metric = 'cargo_pilot_image_failed_attempts' AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
