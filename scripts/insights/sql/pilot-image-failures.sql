SELECT labels['image'] AS image, labels['image_type'] AS image_type, labels['frappe_branch'] AS frappe_branch,
  labels['signup_app'] AS signup_app, labels['pilot_version'] AS pilot_version, labels['stage'] AS stage,
  labels['reason'] AS reason, if(labels['manually_failed'] = '1', 'Yes', 'No') AS manually_failed, value AS retries
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_failed_attempts'
  AND ts = (SELECT max(ts) FROM samples WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 7 DAY)
ORDER BY image DESC
