WITH
batch AS (
  SELECT max(ts) AS at FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 7 DAY
),
usable AS (
  SELECT labels['image_type'] AS image_type, labels['frappe_branch'] AS frappe_branch, labels['signup_app'] AS signup_app,
    value / 3600 AS usable_age_hours
  FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_newest_completed_age_seconds' AND ts = (SELECT at FROM batch)
)
SELECT latest.labels['image_type'] AS image_type, latest.labels['frappe_branch'] AS frappe_branch,
  latest.labels['signup_app'] AS signup_app, latest.labels['image'] AS image, latest.labels['status'] AS status,
  latest.labels['pilot_version'] AS pilot_version, usable.usable_age_hours AS usable_age_hours
FROM samples AS latest
LEFT JOIN usable ON usable.image_type = latest.labels['image_type'] AND usable.frappe_branch = latest.labels['frappe_branch']
  AND usable.signup_app = latest.labels['signup_app']
WHERE latest.resource_id LIKE 'cargo:%' AND latest.metric = 'cargo_pilot_image_latest' AND latest.ts = (SELECT at FROM batch)
ORDER BY image_type, frappe_branch, signup_app
SETTINGS join_use_nulls = 1
