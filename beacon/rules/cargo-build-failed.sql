-- title: Cargo image build failed
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- Cargo sends the newest build of each image every hour. The alert stays while that newest build is Failed.
SELECT 'cargo' AS resource_id, key AS alert_key, 1 AS value,
       concat('the newest ', key, ' image build failed: ', image) AS detail
FROM (
    SELECT concat(labels['image_type'], ' ', labels['frappe_branch'], if(labels['signup_app'] = '', '', concat(' ', labels['signup_app']))) AS key,
           argMax(labels['status'], ts) AS status, argMax(labels['image'], ts) AS image
    FROM datum.samples_recent
    WHERE metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 3 HOUR
    GROUP BY key
)
WHERE status = 'Failed'
