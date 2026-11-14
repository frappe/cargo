-- title: Cargo images stale
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 900
-- pending_for: 900
-- resolve_after: 3600
-- No failure reports a build loop that stopped, so the age of the newest usable image does.
-- Only Base and Site images count. Cargo builds them on a clock. It builds an Apps image only when the app makes a new release, so an old Apps image is normal.
SELECT 'cargo' AS resource_id, key AS alert_key, age AS value,
       concat('the newest usable ', key, ' image is ', toString(round(age / 86400, 1)), ' days old') AS detail
FROM (
    SELECT concat(labels['image_type'], ' ', labels['frappe_branch'], if(labels['signup_app'] = '', '', concat(' ', labels['signup_app']))) AS key,
           argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'cargo_pilot_image_newest_completed_age_seconds' AND resource_id = 'cargo' AND ts > now() - INTERVAL 3 HOUR
      AND labels['image_type'] IN ('Base', 'Site')
    GROUP BY key
)
WHERE age > 2 * 86400
