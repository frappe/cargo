SELECT ts AS time,
  concat(labels['image_type'], ' ', labels['frappe_branch'], if(labels['signup_app'] = '', '', concat(' ', labels['signup_app']))) AS series,
  value / 3600 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_newest_completed_age_seconds' AND ts > now() - INTERVAL 7 DAY
