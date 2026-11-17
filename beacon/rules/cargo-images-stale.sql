-- title: Cargo images are out of date
-- description: The newest usable image is over 2 days old. New VMs get an old image.
-- severity: Medium
-- resource_type: Cargo Server
-- charts: cargo-image-age
-- interval: 900
-- pending_for: 900
-- resolve_after: 3600
-- No failure reports a build loop that stopped, so the age of the newest usable image does.
-- Cargo sends the age only while Track Pilot Releases is on, so a person who turns tracking off gets no alert.
SELECT 'cargo' AS resource_id, key AS alert_key, age AS value,
       concat('newest usable ', key, ' image is ', toString(round(age / 86400, 1)), ' days old (limit 2 days)') AS detail
FROM (
    SELECT concat(labels['image_type'], ' ', labels['frappe_branch'], if(labels['signup_app'] = '', '', concat(' ', labels['signup_app']))) AS key,
           argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'cargo_pilot_image_newest_completed_age_seconds' AND resource_id = 'cargo' AND ts > now() - INTERVAL 3 HOUR
    GROUP BY key
)
WHERE age > 2 * 86400
