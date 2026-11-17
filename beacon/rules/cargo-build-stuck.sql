-- title: Cargo image build takes over 2 hours
-- description: An image build has run for over 2 hours. New VMs of that image wait.
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- Only the latest hourly report counts, because Cargo sends no sample for a build that is done.
SELECT 'cargo' AS resource_id, image AS alert_key, age AS value,
       concat(image_type, ' image ', image, ' is ', status, ' for ', toString(round(age / 3600, 1)), ' h (limit 2 h)') AS detail
FROM (
    SELECT labels['image'] AS image, max(value) AS age, any(labels['image_type']) AS image_type, any(labels['status']) AS status
    FROM datum.samples_recent
    WHERE metric = 'cargo_pilot_image_in_progress_seconds' AND resource_id = 'cargo'
      AND ts = (SELECT max(ts) FROM datum.samples_recent
                WHERE metric LIKE 'cargo_pilot_image_%' AND resource_id = 'cargo' AND ts > now() - INTERVAL 2 HOUR)
    GROUP BY image
)
WHERE age > 7200
