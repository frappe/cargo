-- title: Cargo image build failed
-- description: The newest build of an image failed. New VMs of that image cannot be created.
-- severity: Medium
-- resource_type: Cargo Server
-- charts: cargo-failed-builds
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- Cargo sends the newest build of each image that Cargo Settings lists, every hour. The alert stays while that newest build is Failed.
SELECT 'cargo' AS resource_id, key AS alert_key, 1 AS value,
       concat('newest ', key, ' build failed: ', image) AS detail
FROM (
    SELECT concat(labels['image_type'], ' ', labels['frappe_branch'], if(labels['signup_app'] = '', '', concat(' ', labels['signup_app']))) AS key,
           argMax(labels['status'], ts) AS status, argMax(labels['image'], ts) AS image
    FROM datum.samples_recent
    WHERE metric = 'cargo_pilot_image_latest' AND resource_id = 'cargo' AND ts > now() - INTERVAL 3 HOUR
    GROUP BY key
)
WHERE status = 'Failed'
