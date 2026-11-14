-- title: Cargo image build stuck
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- Only the latest hourly report counts, because Cargo sends no sample for a build that is done.
SELECT 'cargo' AS resource_id, labels['image'] AS alert_key, max(value) AS value,
       concat(any(labels['image_type']), ' image ', alert_key, ' is ', any(labels['status']), ' for ',
              toString(round(value / 3600, 1)), ' h') AS detail
FROM datum.samples_recent
WHERE metric = 'cargo_pilot_image_in_progress_seconds'
  AND ts = (SELECT max(ts) FROM datum.samples_recent WHERE metric LIKE 'cargo_pilot_image_%' AND ts > now() - INTERVAL 2 HOUR)
GROUP BY alert_key
HAVING value > 7200
