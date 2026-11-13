SELECT labels['image'] AS image, labels['image_type'] AS image_type, labels['frappe_branch'] AS frappe_branch,
  labels['signup_app'] AS signup_app, labels['status'] AS status, value / 60 AS idle_minutes
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_in_progress_seconds'
  AND ts = (SELECT max(ts) FROM samples WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 7 DAY)
ORDER BY idle_minutes DESC
